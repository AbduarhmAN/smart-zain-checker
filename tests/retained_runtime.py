"""Process-lifetime test guards: keep fixture files and reject external traffic."""
import functools
import os
from pathlib import Path
import shutil
import socket
import tempfile
from unittest.mock import patch

_patches = []
retained_cleanup = []

def install():
    if _patches:
        return retained_cleanup
    original_connection = socket.create_connection
    original_named = tempfile.NamedTemporaryFile

    def retain(*args, **kwargs):
        if args:
            retained_cleanup.append(str(args[0]))

    def local_connection(address, *args, **kwargs):
        if address[0] not in ('127.0.0.1', 'localhost', '::1'):
            raise OSError('External network is disabled in scheduler verification')
        return original_connection(address, *args, **kwargs)

    @functools.wraps(original_named)
    def retained_named(*args, **kwargs):
        kwargs['delete'] = False
        return original_named(*args, **kwargs)

    _patches.extend([patch.object(shutil, 'rmtree', retain), patch.object(os, 'remove', retain),
        patch.object(os, 'unlink', retain), patch.object(os, 'rmdir', retain),
        patch.object(Path, 'unlink', retain), patch.object(tempfile, 'NamedTemporaryFile', retained_named),
        patch.object(socket, 'create_connection', local_connection)])
    for guard in _patches:
        guard.start()
    return retained_cleanup
