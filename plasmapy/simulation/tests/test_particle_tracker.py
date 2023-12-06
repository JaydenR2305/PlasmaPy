"""
Tests for particle_tracker.py
"""
import astropy.units as u
import numpy as np
import pytest

from hypothesis import given, settings
from hypothesis import strategies as st

from plasmapy.formulary.lengths import gyroradius
from plasmapy.particles import CustomParticle
from plasmapy.plasma import Plasma
from plasmapy.plasma.grids import CartesianGrid
from plasmapy.simulation.particle_tracker import (
    IntervalSaveRoutine,
    NoParticlesOnGridsTerminationCondition,
    ParticleTracker,
    TimeElapsedTerminationCondition,
)

rng = np.random.default_rng()


@pytest.fixture()
def no_particles_on_grids_instantiated():
    return NoParticlesOnGridsTerminationCondition()


@pytest.fixture()
def time_elapsed_termination_condition_instantiated():
    return TimeElapsedTerminationCondition(1 * u.s)


@pytest.fixture()
def disk_interval_save_routine_instantiated(tmp_path):
    return IntervalSaveRoutine(1 * u.s, output_directory=tmp_path)


@pytest.fixture()
def memory_interval_save_routine_instantiated():
    return IntervalSaveRoutine(1 * u.s)


@pytest.fixture()
def grid_with_inf_entry():
    grid = CartesianGrid(-1 * u.m, 1 * u.m)
    entry = np.full(grid.shape, np.NaN) * u.V / u.m
    grid.add_quantities(E_x=entry)

    return grid


@pytest.mark.parametrize(
    ("grids", "termination_condition", "save_routine", "expected_exception"),
    [
        # Old ParticleTracker construction deprecation error
        (
            Plasma(
                domain_x=np.linspace(-1, 1, 10) * u.m,
                domain_y=np.linspace(-1, 1, 10) * u.m,
                domain_z=np.linspace(-1, 1, 10) * u.m,
            ),
            "no_particles_on_grids_instantiated",
            None,
            TypeError,
        ),
        # Unrecognized grid type
        (42, "time_elapsed_termination_condition_instantiated", None, TypeError),
        # Infinite/NaN entry in grid object
        ("grid_with_inf_entry", "no_particles_on_grids_instantiated", None, ValueError),
    ],
)
def test_particle_tracker_constructor_errors(
    request, grids, termination_condition, save_routine, expected_exception
):
    if isinstance(grids, str):
        grids = request.getfixturevalue(grids)

    if termination_condition is not None:
        termination_condition = request.getfixturevalue(termination_condition)

    if save_routine is not None:
        save_routine = request.getfixturevalue(save_routine)

    with pytest.raises(expected_exception):
        ParticleTracker(grids, termination_condition, save_routine)


@pytest.mark.parametrize(
    ("grids", "termination_condition", "save_routine", "kwargs"),
    [
        (
            [CartesianGrid(-2 * u.m, 1 * u.m), CartesianGrid(-1 * u.m, 2 * u.m)],
            "no_particles_on_grids_instantiated",
            None,
            {},
        ),
        (
            CartesianGrid(-1 * u.m, 1 * u.m),
            "no_particles_on_grids_instantiated",
            None,
            {"req_quantities": ["rho"]},
        ),
    ],
)
def test_particle_tracker_construction(
    request, grids, termination_condition, save_routine, kwargs
):
    termination_condition = request.getfixturevalue(termination_condition)

    if save_routine is not None:
        save_routine = request.getfixturevalue(save_routine)

    ParticleTracker(grids, termination_condition, save_routine, **kwargs)


def test_particle_tracker_load_particles_shape_error(
    no_particles_on_grids_instantiated
):
    """Inconsistent shape for x and v error"""
    grid = CartesianGrid(-1 * u.m, 1 * u.m)

    simulation = ParticleTracker(grid, no_particles_on_grids_instantiated)

    with pytest.raises(ValueError):
        simulation.load_particles([[0, 0, 0]] * u.m, [[0, 0, 0], [0, 0, 0]] * u.m / u.s)


@pytest.mark.parametrize(
    ("stop_condition", "save_routine"),
    [
        (
            "no_particles_on_grids_instantiated",
            "memory_interval_save_routine_instantiated",
        ),
        (
            "time_elapsed_termination_condition_instantiated",
            "memory_interval_save_routine_instantiated",
        ),
        (
            "no_particles_on_grids_instantiated",
            "disk_interval_save_routine_instantiated",
        ),
        (
            "time_elapsed_termination_condition_instantiated",
            "disk_interval_save_routine_instantiated",
        ),
    ],
)
def test_interval_save_routine(request, stop_condition, save_routine):
    x = [[0, 0, 0]] * u.m
    v = [[0, 1, 0]] * u.m / u.s
    point_particle = CustomParticle(1 * u.kg, 1 * u.C)

    L = 1 * u.m
    num = 2
    grid = CartesianGrid(-L, L, num=num)
    grid_shape = (num,) * 3

    Ex = np.full(grid_shape, 1) * u.V / u.m
    grid.add_quantities(E_x=Ex)

    termination_condition = request.getfixturevalue(stop_condition)
    save_routine = request.getfixturevalue(save_routine)

    simulation = ParticleTracker(grid, termination_condition, save_routine)
    simulation.load_particles(x, v, point_particle)

    simulation.run()


class TestParticleTrackerGyroradius:
    v_x = rng.integers(1, 10, size=100) * u.m / u.s

    v = np.array([[v_x_element.value, 0, 0] for v_x_element in v_x]) * u.m / u.s

    B_strength = rng.integers(1, 10) * u.T

    point_particle = CustomParticle(1 * u.kg, 1 * u.C)

    # Set the initial position to the gyroradius
    # This means the particle will orbit the origin
    R_L = gyroradius(B_strength, point_particle, Vperp=v_x)
    x = np.array([[0, R_L_element.value, 0] for R_L_element in R_L]) * u.m

    L = 1e6 * u.km
    num = 2
    grid = CartesianGrid(-L, L, num=num)
    grid_shape = (num,) * 3

    Bz = np.full(grid_shape, B_strength) * u.T
    grid.add_quantities(B_z=Bz)

    termination_condition = TimeElapsedTerminationCondition(6 * u.s)
    save_routine = IntervalSaveRoutine(0.1 * u.s)

    simulation = ParticleTracker(
        grid, termination_condition, save_routine, dt=1e-2 * u.s
    )
    simulation.load_particles(x, v, point_particle)

    simulation.run()

    def test_gyroradius(self):
        """Test to ensure particles maintain their gyroradius over time"""
        positions = np.asarray(self.save_routine.r_all) * u.m
        distances = np.linalg.norm(positions, axis=-1)

        assert np.isclose(distances, self.R_L, rtol=5e-2).all()

    def test_kinetic_energy(self):
        """Test to ensure particles maintain their gyroradius over time"""

        initial_kinetic_energies = 0.5 * self.point_particle.mass * self.v_x**2

        velocities = np.asarray(self.save_routine.v_all) * u.m / u.s
        speeds = np.linalg.norm(velocities, axis=-1)
        simulation_kinetic_energies = 0.5 * self.point_particle.mass * speeds**2

        assert np.isclose(initial_kinetic_energies, simulation_kinetic_energies).all()


@given(st.integers(1, 10), st.integers(1, 10), st.integers(1, 10), st.integers(1, 10))
@settings(deadline=2e4, max_examples=10)
def test_particle_tracker_potential_difference(request, E_strength, L, mass, charge):
    # Apply appropriate units to the random inputs
    E_strength = E_strength * u.V / u.m
    L = L * u.m
    mass = mass * u.kg
    charge = charge * u.C

    num = 2
    dt = 1e-2 * u.s

    grid = CartesianGrid(-L, L, num=num)
    grid_shape = (num,) * 3

    Ex = np.full(grid_shape, E_strength) * u.V / u.m
    grid.add_quantities(E_x=Ex)

    point_particle = CustomParticle(mass, charge)

    x = [[0, 0, 0]] * u.m
    v = [[0, 0, 0]] * u.m / u.s

    termination_condition = request.getfixturevalue(
        "no_particles_on_grids_instantiated"
    )
    save_routine = request.getfixturevalue("memory_interval_save_routine_instantiated")

    simulation = ParticleTracker(grid, termination_condition, save_routine, dt=dt)
    simulation.load_particles(x, v, point_particle)

    simulation.run()

    velocities = np.asarray(save_routine.v_all)[:, 0] * u.m / u.s
    speeds = np.linalg.norm(velocities, axis=-1)

    # Final energy is given by the product of the charge and potential difference
    final_expected_energy = (E_strength * L * point_particle.charge).to(u.J)
    final_simulated_energy = (0.5 * point_particle.mass * speeds[-1] ** 2).to(u.J)

    assert np.isclose(
        final_expected_energy, final_simulated_energy, atol=0.5, rtol=5e-2
    )


def test_particle_tracker_stop_particles(request):
    E_strength = 1 * u.V / u.m
    L = 1 * u.m
    mass = 1 * u.kg
    charge = 1 * u.C

    num = 2
    dt = 1e-2 * u.s

    grid = CartesianGrid(-L, L, num=num)
    grid_shape = (num,) * 3

    Ex = np.full(grid_shape, E_strength) * u.V / u.m
    grid.add_quantities(E_x=Ex)

    point_particle = CustomParticle(mass, charge)

    x = [[0, 0, 0]] * u.m
    v = [[0, 0, 0]] * u.m / u.s

    termination_condition = request.getfixturevalue(
        "no_particles_on_grids_instantiated"
    )
    save_routine = request.getfixturevalue("memory_interval_save_routine_instantiated")

    simulation = ParticleTracker(grid, termination_condition, save_routine, dt=dt)
    simulation.load_particles(x, v, point_particle)

    simulation.run()
    simulation._stop_particles([True])

    # Number of entries in mask must be equal to number of particles
    with pytest.raises(ValueError):
        simulation._stop_particles([True, True])

    assert np.isnan(simulation.v[0, :]).all()
    assert not np.isnan(simulation.x[0, :]).all()

    simulation._remove_particles([True])

    # Number of entries in mask must be equal to number of particles
    with pytest.raises(ValueError):
        simulation._remove_particles([True, True])

    assert np.isnan(simulation.x[0, :]).all()
