"""
=====
Usage
=====

Method #1: auxlib.packaging as a run time dependency
---------------------------------------------------

Place the following lines in your package's main __init__.py

from auxlib import get_version
__version__ = get_version(__file__)



Method #2: auxlib.packaging as a build time-only dependency
----------------------------------------------------------


import auxlib

# When executing the setup.py, we need to be able to import ourselves, this
# means that we need to add the src directory to the sys.path.
here = os.path.abspath(os.path.dirname(__file__))
src_dir = os.path.join(here, "auxlib")
sys.path.insert(0, src_dir)

setup(
    version=auxlib.__version__,
    cmdclass={
        'build_py': auxlib.BuildPyCommand,
        'sdist': auxlib.SDistCommand,
        'test': auxlib.Tox,
    },
)



Place the following lines in your package's main __init__.py

from auxlib import get_version
__version__ = get_version(__file__)


Method #3: write .version file
------------------------------



Configuring `python setup.py test` for Tox
------------------------------------------

must use setuptools (distutils doesn't have a test cmd)

setup(
    version=auxlib.__version__,
    cmdclass={
        'build_py': auxlib.BuildPyCommand,
        'sdist': auxlib.SDistCommand,
        'test': auxlib.Tox,
    },
)
"""
from collections import namedtuple
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist
from fnmatch import fnmatchcase
from logging import getLogger
from os import curdir, getenv, listdir, remove, sep
from os.path import abspath, dirname, expanduser, isdir, isfile, join
from re import compile
from subprocess import CalledProcessError, PIPE, Popen

from .compat import shlex_split_unicode
from ..deprecations import deprecated

deprecated.module(
    "23.9",
    "24.3",
    addendum=(
        "Use a modern build systems instead, see https://packaging.python.org/en/latest/tutorials/"
        "packaging-projects#creating-pyproject-toml for more details."
    ),
)

log = getLogger(__name__)

Response = namedtuple('Response', ['stdout', 'stderr', 'rc'])
GIT_DESCRIBE_REGEX = compile(r"(?:[_-a-zA-Z]*)"
                             r"(?P<version>[a-zA-Z0-9.]+)"
                             r"(?:-(?P<post>\d+)-g(?P<hash>[0-9a-f]{7,}))$")


def call(command, path=None, raise_on_error=True):
    path = sys.prefix if path is None else abspath(path)
    p = Popen(shlex_split_unicode(command), cwd=path, stdout=PIPE, stderr=PIPE)
    stdout, stderr = p.communicate()
    rc = p.returncode
    log.debug(
        "{} $  {}\n"
        "  stdout: {}\n"
        "  stderr: {}\n"
        "  rc: {}".format(path, command, stdout, stderr, rc)
    )
    if raise_on_error and rc != 0:
        raise CalledProcessError(rc, command, f"stdout: {stdout}\nstderr: {stderr}")
    return Response(stdout.decode('utf-8'), stderr.decode('utf-8'), int(rc))


def _get_version_from_version_file(path):
    file_path = join(path, '.version')
    if isfile(file_path):
        with open(file_path) as fh:
            return fh.read().strip()


def _git_describe_tags(path):
    try:
        call("git update-index --refresh", path, raise_on_error=False)
    except CalledProcessError as e:
        # git is probably not installed
        log.warn(repr(e))
        return None
    response = call("git describe --tags --long", path, raise_on_error=False)
    if response.rc == 0:
        return response.stdout.strip()
    elif response.rc == 128 and "no names found" in response.stderr.lower():
        # directory is a git repo, but no tags found
        return None
    elif response.rc == 128 and "not a git repository" in response.stderr.lower():
        return None
    elif response.rc == 127:
        log.error("git not found on path: PATH={}".format(getenv("PATH", None)))
        raise CalledProcessError(response.rc, response.stderr)
    else:
        raise CalledProcessError(response.rc, response.stderr)


def _get_version_from_git_tag(tag):
    """Return a PEP440-compliant version derived from the git status.
    If that fails for any reason, return the changeset hash.
    """
    m = GIT_DESCRIBE_REGEX.match(tag)
    if m is None:
        return None
    version, post_commit, hash = m.groups()
    return version if post_commit == "0" else f"{version}.post{post_commit}+{hash}"


def _get_version_from_git_clone(path):
    tag = _git_describe_tags(path) or ''
    return _get_version_from_git_tag(tag)


def get_version(dunder_file):
    """Returns a version string for the current package, derived
    either from git or from a .version file.

    This function is expected to run in two contexts. In a development
    context, where .git/ exists, the version is pulled from git tags.
    Using the BuildPyCommand and SDistCommand classes for cmdclass in
    setup.py will write a .version file into any dist.

    In an installed context, the .version file written at dist build
    time is the source of version information.

    """
    path = abspath(expanduser(dirname(dunder_file)))
    try:
        return _get_version_from_version_file(path) or _get_version_from_git_clone(path)
    except CalledProcessError as e:
        log.warn(repr(e))
        return None
    except Exception as e:
        log.exception(e)
        return None


def write_version_into_init(target_dir, version):
    target_init_file = join(target_dir, "__init__.py")
    assert isfile(target_init_file), f"File not found: {target_init_file}"
    with open(target_init_file) as f:
        init_lines = f.readlines()
    for q in range(len(init_lines)):
        if init_lines[q].startswith('__version__'):
            init_lines[q] = f'__version__ = "{version}"\n'
        elif (init_lines[q].startswith(('from auxlib', 'import auxlib'))
              or 'auxlib.packaging' in init_lines[q]):
            init_lines[q] = None
    print(f"UPDATING {target_init_file}")
    remove(target_init_file)
    with open(target_init_file, "w") as f:
        f.write("".join(filter(None, init_lines)))


def write_version_file(target_dir, version):
    assert isdir(target_dir), f"Directory not found: {target_dir}"
    target_file = join(target_dir, ".version")
    print(f"WRITING {target_file} with version {version}")
    with open(target_file, 'w') as f:
        f.write(version)


class BuildPyCommand(build_py):
    def run(self):
        build_py.run(self)
        target_dir = join(self.build_lib, self.distribution.metadata.name)
        write_version_into_init(target_dir, self.distribution.metadata.version)
        write_version_file(target_dir, self.distribution.metadata.version)
        # TODO: separate out .version file implementation


class SDistCommand(sdist):
    def make_release_tree(self, base_dir, files):
        sdist.make_release_tree(self, base_dir, files)
        target_dir = join(base_dir, self.distribution.metadata.name)
        write_version_into_init(target_dir, self.distribution.metadata.version)
        write_version_file(target_dir, self.distribution.metadata.version)


def convert_path(pathname):
    """Return 'pathname' as a name that will work on the native filesystem,
    i.e. split it on '/' and put it back together again using the current
    directory separator.  Needed because filenames in the setup script are
    always supplied in Unix style, and have to be converted to the local
    convention before we can actually use them in the filesystem.  Raises
    ValueError on non-Unix-ish systems if 'pathname' either starts or
    ends with a slash.

    Copied from setuptools._distutils.util: https://github.com/pypa/setuptools/blob/b545fc778583f644d6c331773dbe0ea53bfa41af/setuptools/_distutils/util.py#L125-L148
    """
    if sep == '/':
        return pathname
    if not pathname:
        return pathname
    if pathname[0] == '/':
        raise ValueError("path '%s' cannot be absolute" % pathname)
    if pathname[-1] == '/':
        raise ValueError("path '%s' cannot end with '/'" % pathname)

    paths = pathname.split('/')
    while '.' in paths:
        paths.remove('.')
    if not paths:
        return curdir
    return join(*paths)


# swiped from setuptools
def find_packages(where='.', exclude=()):
    out = []
    stack = [(convert_path(where), '')]
    while stack:
        where, prefix = stack.pop(0)
        for name in listdir(where):
            fn = join(where, name)
            if "." not in name and isdir(fn) and isfile(join(fn, "__init__.py")):
                out.append(prefix + name)
                stack.append((fn, prefix + name + '.'))
    for pat in list(exclude) + ['ez_setup', 'distribute_setup']:
        out = [item for item in out if not fnmatchcase(item, pat)]
    return out


if __name__ == "__main__":
    # rewrite __init__.py in target_dir
    target_dir = abspath(sys.argv[1])
    version = get_version(join(target_dir, "__init__.py"))
    write_version_into_init(target_dir, version)
