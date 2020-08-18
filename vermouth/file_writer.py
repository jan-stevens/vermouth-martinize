# Copyright 2020 University of Groningen
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from builtins import open as _open

import collections
import os
import pathlib
import shutil
import threading
import tempfile

from .log_helpers import StyleAdapter, get_logger

LOGGER = StyleAdapter(get_logger(__name__))

lock = threading.Lock()


class Singleton(type):
    """
    Metaclass for creating singleton objects. Taken from [1]_.

    .. [1] https://stackoverflow.com/questions/50566934/why-is-this-singleton-implementation-not-thread-safe/50567397
    """
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class DeferredFileWriter(metaclass=Singleton):
    """
    A singleton class/object that is intended to prevent writing output to files
    that is invalid, due to e.g. warnings further down the pipeline.

    If this class is used to open a file for writing, a temporary file is created
    and returned instead. Once it's clear the output produced is valid the
    :meth:`~vermouth.file_writer.DeferredFileWriter.write` method can be used to
    finalize the written changes by moving them to their intended destination.
    If a file with that name already exists it is backed up according to the
    Gromacs scheme.
    """
    def __init__(self):
        self.open_files = collections.deque()
        self._tmpdir = None

    def open(self, filename, mode='r', *args, **kwargs):
        """
        If mode is either 'w' or 'a', opens and returns a handle to a temporary
        file. If mode is 'r' opens and returns a handle to the file specified.

        Once :meth:`~vermouth.file_writer.DeferredFileWriter.write` is called
        the changes written to all files opened this way are propagated to their
        final destination.

        Parameters
        ----------
        filename: os.PathLike
            The final name of the file to be opened.
        mode: str
            The mode in which the file is to be opened.
        *args: collections.abc.Iterable
            Passed to :func:`os.fdopen`.
        **kwargs: dict
            Passed to :func:`os.fdopen`.

        Returns
        -------
        io.IOBase
            An opened file
        """
        if '+' in mode or 'a' in mode or 'w' in mode:  # Append and write
            return self._open_tmp_file(filename, *args, mode=mode, **kwargs)
        elif 'r' in mode:  # Read
            return _open(filename, *args, mode=mode, **kwargs)
        raise KeyError('Unknown file mode.')

    def _open_tmp_file(self, filename, mode='w', *args, **kwargs):
        path = pathlib.Path(filename)
        # Make the path absolute, in case the current working directory is
        # changed between now and writing.
        path = path.parent.resolve() / path.name
        suffix = path.suffix
        with lock:
            handle, tmp_path = tempfile.mkstemp(suffix=suffix, dir=self._tmpdir)
        self.open_files.append([tmp_path, str(path), mode])
        if '+' in mode:
            shutil.copy2(filename, tmp_path)
        return os.fdopen(handle, *args, mode=mode, **kwargs)

    @staticmethod
    def _find_free_path(file_path):
        """
        Find the first free (backup) path that looks like `file_path`. If
        file_path does not exist, returns file_path. Else, generates
        `#{name}.{idx}#`, incrementing idx until no file exists.

        Parameters
        ----------
        file_path: pathlib.PathLike

        Returns
        -------
        pathlib.Path
            The first path that does not exist yet
        """
        file_path = pathlib.Path(file_path)
        backup_path = pathlib.Path(file_path)
        idx = 1
        while backup_path.exists():
            backup_path = file_path.with_name('#{name}.{idx}#'.format(name=file_path.name, idx=idx))
            idx += 1
        return backup_path

    def write(self):
        """
        Finalize writing all open files by moving the created temporary files to
        their final destinations.

        Existing file destinations will be backed up according to the Gromacs
        scheme.
        """
        while self.open_files:
            tmp_path, final_path, mode = self.open_files.popleft()
            if 'w' in mode or '+' in mode:  # write
                self._write_file(tmp_path, final_path)
            elif 'r' in mode:  # read = error
                raise AssertionError("Files opened with mode 'r' should not be "
                                     "treated special")
            elif 'a' in mode:  # append
                self._append_file(tmp_path, final_path, mode)
            else:
                raise KeyError('Unknown file mode')

    def _write_file(self, tmp_path, final_path):
        # There is no way to move a file and make it error if the destination
        # already exists, so use a lock instead.
        with lock:
            free_path = str(self._find_free_path(final_path))
            if free_path != final_path:
                LOGGER.info('Backing up {} to {}.', final_path, free_path, type='general')
                shutil.move(final_path, free_path)
            LOGGER.debug('Writing output to {}.', final_path, type='general')
            shutil.move(tmp_path, final_path)

    @staticmethod
    def _append_file(tmp_path, final_path, mode='a'):
        """
        Append the contents of tmp_path to final_path and remove tmp_path.
        """
        if 'b' in mode:
            tmp_mode = 'rb'
        else:
            tmp_mode = 'r'
        with _open(final_path, mode=mode) as final_file, _open(tmp_path, mode=tmp_mode) as tmp_file:
            final_file.write(tmp_file.read())
        os.remove(tmp_path)

    def close(self):
        """
        Remove all produced temporary files.
        """
        while self.open_files:
            tmp_path, *_ = self.open_files.popleft()
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass

    def __del__(self):
        self.close()
        # super().__del__()  # object has no __del__


open = DeferredFileWriter().open
