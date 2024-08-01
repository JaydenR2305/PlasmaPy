"""
This module contains functionality for auto-generating the stub files related to
the :rst:dir:`automodapi` and :rst:dir:`automodsumm` directives.
"""
__all__ = ["AutomodsummEntry", "AutomodsummRenderer", "GenDocsFromAutomodsumm"]

import os
import re

from jinja2 import TemplateNotFound
from sphinx.ext.autodoc.mock import mock
from sphinx.ext.autosummary import get_rst_suffix, import_by_name, import_ivar_by_name
from sphinx.ext.autosummary.generate import (
    find_autosummary_in_files,
    AutosummaryEntry,
    AutosummaryRenderer,
    generate_autosummary_content,
)
from sphinx.locale import __
from sphinx.util import logging
from sphinx.util.osutil import ensuredir
from typing import Any, Dict, List, Union

from ..utils import templates_dir

if False:
    # noqa
    # for annotation, does not need real import
    from sphinx.application import Sphinx
    from sphinx.builders import Builder


logger = logging.getLogger(__name__)


class AutomodsummEntry(AutosummaryEntry):
    """
    A typed version of `~collections.namedtuple` representing an stub file
    entry for :rst:dir:`automodsumm`.

    Parameters
    ----------
    name : `str`
        The objects fully qualified name of the object for which the stub file
        will be generated.

    path : `str`
        Absolute file path to the toctree directory.  This is where the stub
        file will be placed.

    recursive : `bool`
        Specifies if stub file for modules and and sub-packages should be
        generated.

    template : `str`
        Name of the template file to be used in generating the stub file.
    """


class AutomodsummRenderer(AutosummaryRenderer):
    """
    A helper class for retrieving and rendering :rst:dir:`automodsumm` templates
    when writing stub files.

    Parameters
    ----------

    app : `sphinx.application.Sphinx`
        Instance of the `sphinx` application.
    """

    def __init__(self, app: "Sphinx") -> None:
        # add plasmapy_sphinx templates directory to the overall templates path
        asumm_path = templates_dir
        relpath = os.path.relpath(asumm_path, start=app.srcdir)
        app.config.templates_path.append(relpath)

        super().__init__(app)

    def render(self, template_name: str, context: Dict) -> str:
        """
        Render a template file.  The render will first search for the template in
        the path specified by the sphinx configuration value :confval:`templates_path`,
        then the `~plasmapy_sphinx.utils.templates_dir`, and finally the
        :rst:dir:`autosummary` templates directory.  Upon finding the template,
        the values from the ``context`` dictionary will inserted into the
        template and returned.

        Parameters
        ----------
        template_name : str
            Name of the template file.

        context: dict
            Dictionary of values to be rendered (inserted) into the template.
        """
        if not template_name.endswith(".rst"):
            # if does not have '.rst' then objtype likely given for template_name
            template_name += ".rst"

        template = None
        for name in [template_name, "base.rst"]:
            for _path in ["", "automodsumm/", "autosummary/"]:
                try:
                    template = self.env.get_template(_path + name)
                    return template.render(context)
                except TemplateNotFound:
                    pass

        if template is None:
            raise TemplateNotFound


class GenDocsFromAutomodsumm:
    """
    Class used for stub file generation from :rst:dir:`automodapi` and
    :rst:dir:`automodsumm`.  An instance of the class is connected to the Sphinx
    event :event:`builder-inited`, which is emitted when the builder object is
    created.
    """

    _re = {
        "automodsumm": re.compile(r"^\n?(\s*)\.\.\s+automodsumm::\s*(\S+)\s*(?:\n|$)"),
        "automodapi": re.compile(r"^\n?(\s*)\.\.\s+automodapi::\s*(\S+)\s*(?:\n|$)"),
        "option": re.compile(r"^\n?(\s+):(\S*):\s*(\S.*|)\s*(?:\n|$)"),
        "currentmodule": re.compile(
            r"^\s*\.\.\s+(|\S+:)(current)?module::\s*([a-zA-Z0-9_.]+)\s*$"
        ),
    }
    """
    Dictionary of regular expressions used for string matching a read document
    and identify key directives.
    """

    app = None  # type: "Sphinx"
    """Instance of the Sphinx application."""

    logger = logger
    """
    Instance of the `~sphinx.util.logging.SphinxLoggerAdapter` for report during
    builds.
    """

    def __call__(self, app: "Sphinx"):
        """
        Scan through source files, check for the :rst:dir:`automodsumm` and
        :rst:dir:`automodapi` directives, and auto generate any associated
        stub files.

        Parameters
        ----------
        app :  `~sphinx.application.Sphinx`
            Instance of the Sphinx application.


        .. note:: Adapted from :func:`sphinx.ext.autosummary.process_generate_options`.
        """
        self.app = app
        genfiles = app.config.autosummary_generate

        if genfiles is True:
            env = app.builder.env
            genfiles = [
                env.doc2path(x, base=None)
                for x in env.found_docs
                if os.path.isfile(env.doc2path(x))
            ]
        elif genfiles is False:
            pass
        else:
            ext = list(app.config.source_suffix)
            genfiles = [
                genfile + (ext[0] if not genfile.endswith(tuple(ext)) else "")
                for genfile in genfiles
            ]

            for entry in genfiles[:]:
                if not os.path.isfile(os.path.join(app.srcdir, entry)):
                    self.logger.warning(
                        __(f"automodsumm_generate: file not found: {entry}")
                    )
                    genfiles.remove(entry)

        if not genfiles:
            return

        suffix = get_rst_suffix(app)
        if suffix is None:
            self.logger.warning(
                __(
                    "automodsumm generates .rst files internally. "
                    "But your source_suffix does not contain .rst. Skipped."
                )
            )
            return

        imported_members = app.config.autosummary_imported_members
        with mock(app.config.autosummary_mock_imports):
            self.generate_docs(
                genfiles,
                suffix=suffix,
                base_path=app.srcdir,
                imported_members=imported_members,
                overwrite=app.config.autosummary_generate_overwrite,
                encoding=app.config.source_encoding,
            )

    def generate_docs(
        self,
        source_filenames: List[str],
        output_dir: str = None,
        suffix: str = ".rst",
        base_path: str = None,
        imported_members: bool = False,
        overwrite: bool = True,
        encoding: str = "utf-8",
    ) -> None:
        """
        Generate and write stub files for objects defined in the :rst:dir:`automodapi`
        and :rst:dir:`automodsumm` directives.

        Parameters
        ----------

        source_filenames : List[str]
            A list of all filenames for with the :rst:dir:`automodapi` and
            :rst:dir:`automodsumm` directives will be searched for.

        output_dir : `str`
            Directory for which the stub files will be written to.

        suffix : `str`
            (Default ``".rst"``) Suffix given to the written stub files.

        base_path : `str`
            The common base path for the filenames listed in ``source_filenames``.
            This is typically the source directory of the Sphinx application.

        imported_members : `bool`
            (Default `False`) Set `True` to include imported members in the
            stub file documentation for *module* object types.

        overwrite : `bool`
            (Default `True`)  Will cause existing stub files to be overwritten.

        encoding : `str`
            (Default: ``"utf-8"``) Encoding for the written stub files.


        .. note::  Adapted from
                   :func:`sphinx.ext.autosummary.generate.generate_autosummary_docs`.
        """
        app = self.app

        _info = self.logger.info
        _warn = self.logger.warning

        showed_sources = list(sorted(source_filenames))
        _info(
            __(f"[automodsumm] generating stub files for {len(showed_sources)} sources")
        )

        if output_dir:
            _info(__(f"[automodsumm] writing to {output_dir}"))

        if base_path is not None:
            source_filenames = [
                os.path.join(base_path, filename) for filename in source_filenames
            ]

        template = AutomodsummRenderer(app)

        # read
        items = find_autosummary_in_files(source_filenames)

        # keep track of new files
        new_files = []

        if app:
            filename_map = app.config.autosummary_filename_map
        else:
            filename_map = {}

        # write
        for entry in sorted(set(items), key=str):
            if entry.path is None:
                # The corresponding automodsumm:: directive did not have
                # a :toctree: option
                continue

            path = output_dir or os.path.abspath(entry.path)
            ensuredir(path)

            try:
                name, obj, parent, modname = import_by_name(entry.name)
                qualname = name.replace(modname + ".", "")
            except ImportError as e:
                try:
                    # try to import as an instance attribute
                    name, obj, parent, modname = import_ivar_by_name(entry.name)
                    qualname = name.replace(modname + ".", "")
                except ImportError:
                    _warn(__(f"[automodsumm] failed to import {entry.name}: {e}"))
                    continue

            context = {}
            if app:
                context.update(app.config.autosummary_context)

            content = generate_autosummary_content(
                name,
                obj,
                parent,
                template,
                entry.template,
                imported_members,
                app,
                entry.recursive,
                context,
                modname,
                qualname,
            )

            filename = os.path.join(path, filename_map.get(name, name) + suffix)
            if os.path.isfile(filename):
                with open(filename, encoding=encoding) as f:
                    old_content = f.read()

                if content == old_content:
                    continue
                elif overwrite:  # content has changed
                    with open(filename, "w", encoding=encoding) as f:
                        f.write(content)
                    new_files.append(filename)
            else:
                with open(filename, "w", encoding=encoding) as f:
                    f.write(content)
                new_files.append(filename)

        # descend recursively to new files
        if new_files:
            self.generate_docs(
                new_files,
                output_dir=output_dir,
                suffix=suffix,
                base_path=base_path,
                imported_members=imported_members,
                overwrite=overwrite,
            )

    def find_in_files(self, filenames: List[str]) -> List[AutomodsummEntry]:
        """
        Search files for the :rst:dir:`automodapi` and :rst:dir:`automodsumm`
        directives and generate a list of
        `~plasmapy_sphinx.automodsumm.generate.AutomodsummEntry`'s indicating which stub
        files need to be generated.

        Parameters
        ----------
        filenames : List[str]
            List of filenames to be searched.


        .. note:: Adapted from
                  :func:`sphinx.ext.autosummary.generate.find_autosummary_in_files`.
        """
        documented = []  # type: List[AutomodsummEntry]
        for filename in filenames:
            with open(filename, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
                documented.extend(self.find_in_lines(lines, filename=filename))
        return documented

    @staticmethod
    def event_handler__autodoc_skip_member(
        app: "Sphinx", what: str, name: str, obj: Any, skip: bool, options: dict
    ):  # noqa
        """
        Event handler for the Sphinx event :event:`autodoc-skip-member`.  This
        handler ensures the ``__call__`` method is documented if defined by the
        associated class.
        """
        if what != "method":
            return

        if name == "__call__":
            return False
        return
