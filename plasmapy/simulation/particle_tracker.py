"""
Module containing the definition for the general particle tracker.
"""

__all__ = ["ParticleTracker"]

import astropy.units as u
import collections
import numpy as np
import sys
import warnings

from collections.abc import Iterable
from tqdm import tqdm
from typing import Union

from plasmapy.particles import Particle, particle_input
from plasmapy.plasma.grids import AbstractGrid
from plasmapy.simulation.particle_integrators import boris_push


class ParticleTracker:
    """
    General particle tracer.

    """

    def __init__(
        self,
        grids: Union[AbstractGrid, Iterable[AbstractGrid]],
        req_quantities=None,
        verbose=True,
    ):
        # self.grid is the grid object
        if isinstance(grids, AbstractGrid):
            self.grids = [
                grids,
            ]
        elif isinstance(grids, collections.abc.Iterable):
            self.grids = grids
        else:
            raise TypeError("Type of argument `grids` not recognized.")

        # self.grid_arr is the grid positions in si units. This is created here
        # so that it isn't continuously called later
        self.grids_arr = [grid.grid.to(u.m).value for grid in self.grids]

        self.verbose = verbose

        # This flag records whether the simulation has been run
        self._has_run = False

        # *********************************************************************
        # Validate required fields
        # *********************************************************************

        for grid in self.grids:
            grid.require_quantities(req_quantities, replace_with_zeros=True)

            for rq in req_quantities:
                # Check that there are no infinite values
                if not np.isfinite(grid[rq].value).all():
                    raise ValueError(
                        f"Input arrays must be finite: {rq} contains "
                        "either NaN or infinite values."
                    )

                # Check that the max values on the edges of the arrays are
                # small relative to the maximum values on that grid
                #
                # Array must be dimensionless to re-assemble it into an array
                # of max values like this
                arr = np.abs(grid[rq]).value
                edge_max = np.max(
                    np.array(
                        [
                            np.max(a)
                            for a in (
                                arr[0, :, :],
                                arr[-1, :, :],
                                arr[:, 0, :],
                                arr[:, -1, :],
                                arr[:, :, 0],
                                arr[:, :, -1],
                            )
                        ]
                    )
                )

                if edge_max > 1e-3 * np.max(arr):
                    unit = grid.recognized_quantities[rq].unit
                    warnings.warn(
                        "Fields should go to zero at edges of grid to avoid "
                        f"non-physical effects, but a value of {edge_max:.2E} {unit} was "
                        f"found on the edge of the {rq} array. Consider applying a "
                        "envelope function to force the fields at the edge to go to "
                        "zero.",
                        RuntimeWarning,
                    )

    @property
    def num_grids(self):  # noqa: D102
        return len(self.grids)

    def _log(self, msg):
        if self.verbose:
            print(msg)

    @particle_input
    def load_particles(
        self,
        x,
        v,
        particle: Particle = Particle("p+"),  # noqa: B008
    ):
        r"""
        Load arrays of particle positions and velocities.

        Parameters
        ----------
        x : `~astropy.units.Quantity`, shape (N,3)
            Positions for N particles

        v: `~astropy.units.Quantity`, shape (N,3)
            Velocities for N particles

        particle : |particle-like|, optional
            Representation of the particle species as either a |Particle| object
            or a string representation. The default particle is protons.

        distribution: str
            A keyword which determines how particles will be distributed
            in velocity space. Options are:

                - 'monte-carlo': velocities will be chosen randomly,
                    such that the flux per solid angle is uniform.

                - 'uniform': velocities will be distributed such that,
                   left unperturbed,they will form a uniform pattern
                   on the detection plane.

            Simulations run in the ``'uniform'`` mode will imprint a grid pattern
            on the image, but will well-sample the field grid with a
            smaller number of particles. The default is ``'monte-carlo'``.


        """
        # Raise an error if the run method has already been called.
        self._enforce_order()

        self.q = particle.charge.to(u.C).value
        self.m = particle.mass.to(u.kg).value

        if x.shape[0] != v.shape[0]:
            raise ValueError(
                "Provided x and v arrays have inconsistent numbers "
                " of particles "
                f"({x.shape[0]} and {v.shape[0]} respectively)."
            )
        else:
            self.nparticles = x.shape[0]

        self.x = x.to(u.m).value
        self.v = v.to(u.m / u.s).value

    # *************************************************************************
    # Run/push loop methods
    # *************************************************************************

    def _adaptive_dt(self, Ex, Ey, Ez, Bx, By, Bz):  # noqa: ARG002
        r"""
        Calculate the appropriate dt for each grid based on a number of
        considerations
        including the local grid resolution (ds) and the gyroperiod of the
        particles in the current fields.
        """
        # If dt was explicitly set, skip the rest of this function
        if self.dt.size == 1:
            return self.dt

        # candidate timesteps includes one per grid (based on the grid resolution)
        # plus additional candidates based on the field at each particle
        candidates = np.ones([self.nparticles_tracked, self.num_grids + 1]) * np.inf

        # Compute the timestep indicated by the grid resolution
        ds = np.array([grid.grid_resolution.to(u.m).value for grid in self.grids])
        gridstep = 0.5 * (ds / self.vmax)

        # Wherever a particle is on a grid, include that grid's gridstep
        # in the list of candidate timesteps
        for i, _grid in enumerate(self.grids):  # noqa: B007
            candidates[:, i] = np.where(self.on_grid[:, i] > 0, gridstep[i], np.inf)

        # If not, compute a number of possible timesteps
        # Compute the cyclotron gyroperiod
        Bmag = np.max(np.sqrt(Bx**2 + By**2 + Bz**2)).to(u.T).value
        # Compute the gyroperiod
        if Bmag == 0:
            gyroperiod = np.inf
        else:
            gyroperiod = 2 * np.pi * self.m / (self.q * np.max(Bmag))

        candidates[:, self.num_grids] = gyroperiod / 12

        # TODO: introduce a minimum timestep based on electric fields too!

        # Enforce limits on dt
        candidates = np.clip(candidates, self.dt[0], self.dt[1])

        # dt is the min of all the candidates for each particle
        # a separate dt is returned for each particle
        dt = np.min(candidates, axis=-1)

        # dt should never actually be infinite, so replace any infinities
        # with the largest gridstep
        return np.where(dt == np.inf, np.max(gridstep), dt)

    def _push(self):
        r"""
        Advance particles using an implementation of the time-centered
        Boris algorithm.
        """
        # Get a list of positions (input for interpolator)
        pos = self.x * u.m

        # Update the list of particles on and off the grid
        # shape [nparticles, ngrids]
        self.on_grid = np.array([grid.on_grid(pos) for grid in self.grids]).T

        # entered_grid is zero at the end if a particle has never
        # entered any grid
        self.entered_grid += np.sum(self.on_grid, axis=-1)

        Ex = np.zeros(self.nparticles_tracked) * u.V / u.m
        Ey = np.zeros(self.nparticles_tracked) * u.V / u.m
        Ez = np.zeros(self.nparticles_tracked) * u.V / u.m
        Bx = np.zeros(self.nparticles_tracked) * u.T
        By = np.zeros(self.nparticles_tracked) * u.T
        Bz = np.zeros(self.nparticles_tracked) * u.T
        for grid in self.grids:
            # Estimate the E and B fields for each particle
            # Note that this interpolation step is BY FAR the slowest part of the push
            # loop. Any speed improvements will have to come from here.
            if self.field_weighting == "volume averaged":
                _Ex, _Ey, _Ez, _Bx, _By, _Bz = grid.volume_averaged_interpolator(
                    pos,
                    "E_x",
                    "E_y",
                    "E_z",
                    "B_x",
                    "B_y",
                    "B_z",
                    persistent=True,
                )
            elif self.field_weighting == "nearest neighbor":
                _Ex, _Ey, _Ez, _Bx, _By, _Bz = grid.nearest_neighbor_interpolator(
                    pos,
                    "E_x",
                    "E_y",
                    "E_z",
                    "B_x",
                    "B_y",
                    "B_z",
                    persistent=True,
                )

            # Interpret any NaN values (points off the grid) as zero
            # Do this before adding to the totals, because 0 + nan = nan
            _Ex = np.nan_to_num(_Ex, nan=0.0 * u.V / u.m)
            _Ey = np.nan_to_num(_Ey, nan=0.0 * u.V / u.m)
            _Ez = np.nan_to_num(_Ez, nan=0.0 * u.V / u.m)
            _Bx = np.nan_to_num(_Bx, nan=0.0 * u.T)
            _By = np.nan_to_num(_By, nan=0.0 * u.T)
            _Bz = np.nan_to_num(_Bz, nan=0.0 * u.T)

            # Add the values interpolated for this grid to the totals
            Ex += _Ex
            Ey += _Ey
            Ez += _Ez
            Bx += _Bx
            By += _By
            Bz += _Bz

        # Create arrays of E and B as required by push algorithm
        E = np.array(
            [Ex.to(u.V / u.m).value, Ey.to(u.V / u.m).value, Ez.to(u.V / u.m).value]
        )
        E = np.moveaxis(E, 0, -1)
        B = np.array([Bx.to(u.T).value, By.to(u.T).value, Bz.to(u.T).value])
        B = np.moveaxis(B, 0, -1)

        # Calculate the adaptive timestep from the fields currently experienced
        # by the particles
        # If user sets dt explicitly, that's handled in _adaptive_dt
        dt = self._adaptive_dt(Ex, Ey, Ez, Bx, By, Bz)

        # TODO: Test v/c and implement relativistic Boris push when required
        # vc = np.max(v)/_c

        # If dt is not a scalar, make sure it can be multiplied by an
        # [nparticles, 3] shape field array
        if dt.size > 1:
            dt = dt[:, np.newaxis]

        self.x, self.v = boris_push(
            self.x, self.v, B, E, self.q, self.m, dt, inplace=False
        )

    @property
    def on_any_grid(self):
        """
        Binary array for each particle indicating whether it is currently
        on ANY grid.
        """
        return np.sum(self.on_grid, axis=-1) > 0

    @staticmethod
    def _stop_condition():
        return False

    @property
    def vmax(self):
        """
        Calculate the maximum velocity.
        Used for determining the grid crossing maximum timestep.
        """
        return np.max(np.linalg.norm(self.v, axis=-1))

    def _validate_inputs(self, field_weighting: str):
        # Load and validate inputs
        field_weightings = ["volume averaged", "nearest neighbor"]
        if field_weighting in field_weightings:
            self.field_weighting = field_weighting
        else:
            raise ValueError(
                f"{field_weighting} is not a valid option for ",
                "field_weighting. Valid choices are",
                f"{field_weightings}",
            )

        # Check to make sure particles have already been generated
        if not hasattr(self, "x"):
            raise ValueError(
                "Either the create_particles or load_particles method must be "
                "called before running the particle tracing algorithm."
            )

    def run(self, dt=None, field_weighting="volume averaged"):
        r"""
        Runs a particle-tracing simulation.
        Timesteps are adaptively calculated based on the
        local grid resolution of the particles and the electric and magnetic
        fields they are experiencing.

        Parameters
        ----------
        dt : `~astropy.units.Quantity`, optional
            An explicitly set timestep in units convertible to seconds.
            Setting this optional keyword overrules the adaptive time step
            capability and forces the use of this timestep throughout. If a tuple
            of timesteps is provided, the adaptive timestep will be clamped
            between the first and second values.

        field_weighting : str
            String that selects the field weighting algorithm used to determine
            what fields are felt by the particles. Options are:

            * 'nearest neighbor': Particles are assigned the fields on
                the grid vertex closest to them.
            * 'volume averaged' : The fields experienced by a particle are a
                volume-average of the eight grid points surrounding them.

            The default is 'volume averaged'.

        Returns
        -------
        None

        """

        self._validate_inputs(field_weighting)

        self.nparticles_tracked = self.x.shape[0]

        # By default, set dt as an infinite range (auto dt with no restrictions)
        self.dt = np.array([0.0, np.inf]) * u.s if dt is None else dt
        self.dt = (self.dt).to(u.s).value

        # Create flags for tracking when particles during the simulation
        # on_grid -> zero if the particle is off grid, 1
        # shape [nparticles, ngrids]
        self.on_grid = np.zeros([self.nparticles_tracked, self.num_grids])

        # Entered grid -> non-zero if particle EVER entered a grid
        self.entered_grid = np.zeros([self.nparticles_tracked])

        # Initialize a "progress bar" (really more of a meter)
        # Setting sys.stdout lets this play nicely with regular print()
        pbar = tqdm(
            initial=0,
            total=self.nparticles_tracked + 1,
            disable=not self.verbose,
            desc="Particles on grid",
            unit="particles",
            bar_format="{l_bar}{bar}{n:.1e}/{total:.1e} {unit}",  # noqa: FS003
            file=sys.stdout,
        )

        # Push the particles until the stop condition is satisfied
        # (no more particles on the simulation grid)
        while not self._stop_condition():
            n_on_grid = np.sum(self.on_any_grid)
            pbar.n = n_on_grid
            pbar.last_print_n = n_on_grid
            pbar.update()

            self._push()
        pbar.close()

        # Log a summary of the run

        self._log("Run completed")

        # Simulation has not run, because creating new particles changes the simulation
        self._has_run = True

    def _enforce_order(self):
        r"""
        The `Tracker` methods could give strange results if setup methods
        are used again after the simulation has run. This method
        raises an error if the simulation has already been run.

        """

        if self._has_run:
            raise RuntimeError(
                "Modifying the `Tracker` object after running the "
                "simulation is not supported. Create a new `Tracker` "
                "object for a new simulation."
            )