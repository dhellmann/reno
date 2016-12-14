# -*- coding: utf-8 -*-

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import unicode_literals

import itertools
import logging
import os.path
import re
import subprocess
import time

from dulwich import diff_tree
from dulwich import objects
import fixtures
import mock
from testtools.content import text_content

from reno import config
from reno import create
from reno import scanner
from reno.tests import base
from reno import utils


_SETUP_TEMPLATE = """
import setuptools
try:
    import multiprocessing  # noqa
except ImportError:
    pass

setuptools.setup(
    setup_requires=['pbr'],
    pbr=True)
"""

_CFG_TEMPLATE = """
[metadata]
name = testpkg
summary = Test Package

[files]
packages =
    testpkg
"""


class GPGKeyFixture(fixtures.Fixture):
    """Creates a GPG key for testing.

    It's recommended that this be used in concert with a unique home
    directory.
    """

    def setUp(self):
        super(GPGKeyFixture, self).setUp()
        tempdir = self.useFixture(fixtures.TempDir())
        gnupg_version_re = re.compile('^gpg\s.*\s([\d+])\.([\d+])\.([\d+])')
        gnupg_version = utils.check_output(['gpg', '--version'],
                                           cwd=tempdir.path)
        for line in gnupg_version.split('\n'):
            gnupg_version = gnupg_version_re.match(line)
            if gnupg_version:
                gnupg_version = (int(gnupg_version.group(1)),
                                 int(gnupg_version.group(2)),
                                 int(gnupg_version.group(3)))
                break
        else:
            if gnupg_version is None:
                gnupg_version = (0, 0, 0)
        config_file = tempdir.path + '/key-config'
        f = open(config_file, 'wt')
        try:
            if gnupg_version[0] == 2 and gnupg_version[1] >= 1:
                f.write("""
                %no-protection
                %transient-key
                """)
            f.write("""
            %no-ask-passphrase
            Key-Type: RSA
            Name-Real: Example Key
            Name-Comment: N/A
            Name-Email: example@example.com
            Expire-Date: 2d
            Preferences: (setpref)
            %commit
            """)
        finally:
            f.close()
        # Note that --quick-random (--debug-quick-random in GnuPG 2.x)
        # does not have a corresponding preferences file setting and
        # must be passed explicitly on the command line instead
        if gnupg_version[0] == 1:
            gnupg_random = '--quick-random'
        elif gnupg_version[0] >= 2:
            gnupg_random = '--debug-quick-random'
        else:
            gnupg_random = ''
        cmd = ['gpg', '--gen-key', '--batch']
        if gnupg_random:
            cmd.append(gnupg_random)
        cmd.append(config_file)
        subprocess.check_call(
            cmd,
            cwd=tempdir.path,
            # Direct stderr to its own pipe, from which we don't read,
            # to quiet the commands.
            stderr=subprocess.PIPE,
        )


class GitRepoFixture(fixtures.Fixture):

    logger = logging.getLogger('git')

    def __init__(self, reporoot):
        self.reporoot = reporoot
        super(GitRepoFixture, self).__init__()

    def setUp(self):
        super(GitRepoFixture, self).setUp()
        self.useFixture(GPGKeyFixture())
        os.makedirs(self.reporoot)
        self.git('init', '.')
        self.git('config', '--local', 'user.email', 'example@example.com')
        self.git('config', '--local', 'user.name', 'reno developer')
        self.git('config', '--local', 'user.signingkey',
                 'example@example.com')

    def git(self, *args):
        self.logger.debug('$ git %s', ' '.join(args))
        output = utils.check_output(
            ['git'] + list(args),
            cwd=self.reporoot,
        )
        self.logger.debug(output)
        return output

    def commit(self, message='commit message'):
        self.git('add', '.')
        self.git('commit', '-m', message)
        self.git('show', '--pretty=format:%H')
        time.sleep(0.1)  # force a delay between commits

    def add_file(self, name):
        with open(os.path.join(self.reporoot, name), 'w') as f:
            f.write('adding %s\n' % name)
        self.commit('add %s' % name)


class Base(base.TestCase):

    logger = logging.getLogger('test')

    def _add_notes_file(self, slug='slug', commit=True, legacy=False,
                        contents='i-am-also-a-template'):
        n = self.get_note_num()
        if legacy:
            basename = '%016x-%s.yaml' % (n, slug)
        else:
            basename = '%s-%016x.yaml' % (slug, n)
        filename = os.path.join(self.reporoot, 'releasenotes', 'notes',
                                basename)
        create._make_note_file(filename, contents)
        self.repo.commit('add %s' % basename)
        return os.path.join('releasenotes', 'notes', basename)

    def _make_python_package(self):
        setup_name = os.path.join(self.reporoot, 'setup.py')
        with open(setup_name, 'w') as f:
            f.write(_SETUP_TEMPLATE)
        cfg_name = os.path.join(self.reporoot, 'setup.cfg')
        with open(cfg_name, 'w') as f:
            f.write(_CFG_TEMPLATE)
        pkgdir = os.path.join(self.reporoot, 'testpkg')
        os.makedirs(pkgdir)
        init = os.path.join(pkgdir, '__init__.py')
        with open(init, 'w') as f:
            f.write("Test package")
        self.repo.commit('add test package')

    def setUp(self):
        super(Base, self).setUp()
        self.fake_logger = self.useFixture(
            fixtures.FakeLogger(
                format='%(levelname)8s %(name)s %(message)s',
                level=logging.DEBUG,
                nuke_handlers=True,
            )
        )
        # Older git does not have config --local, so create a temporary home
        # directory to permit using git config --global without stepping on
        # developer configuration.
        self.useFixture(fixtures.TempHomeDir())
        self.useFixture(fixtures.NestedTempfile())
        self.temp_dir = self.useFixture(fixtures.TempDir()).path
        self.reporoot = os.path.join(self.temp_dir, 'reporoot')
        self.repo = self.useFixture(GitRepoFixture(self.reporoot))
        self.c = config.Config(self.reporoot)
        self._counter = itertools.count(1)
        self.get_note_num = lambda: next(self._counter)


class BasicTest(Base):

    def test_non_python_no_tags(self):
        filename = self._add_notes_file()
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'0.0.0': [filename]},
            results,
        )

    def test_python_no_tags(self):
        self._make_python_package()
        filename = self._add_notes_file()
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'0.0.0': [filename]},
            results,
        )

    def test_note_before_tag(self):
        filename = self._add_notes_file()
        self.repo.add_file('not-a-release-note.txt')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [filename]},
            results,
        )

    def test_note_commit_tagged(self):
        filename = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [filename]},
            results,
        )

    def test_note_commit_after_tag(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        filename = self._add_notes_file()
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0-1': [filename]},
            results,
        )

    def test_other_commit_after_tag(self):
        filename = self._add_notes_file()
        self.repo.add_file('ignore-1.txt')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.repo.add_file('ignore-2.txt')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [filename]},
            results,
        )

    def test_multiple_notes_after_tag(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file()
        f2 = self._add_notes_file()
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0-2': [f1, f2]},
            results,
        )

    def test_multiple_notes_within_tag(self):
        self._make_python_package()
        f1 = self._add_notes_file(commit=False)
        f2 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [f1, f2]},
            results,
        )

    def test_multiple_tags(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        f2 = self._add_notes_file()
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f1],
             '2.0.0-1': [f2],
             },
            results,
        )

    def test_rename_file(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        f2 = f1.replace('slug1', 'slug2')
        self.repo.git('mv', f1, f2)
        self.repo.commit('rename note file')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f2],
             },
            results,
        )

    def test_rename_file_sort_earlier(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        f2 = f1.replace('slug1', 'slug0')
        self.repo.git('mv', f1, f2)
        self.repo.commit('rename note file')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f2],
             },
            results,
        )

    def test_edit_file(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        with open(os.path.join(self.reporoot, f1), 'w') as f:
            f.write('---\npreamble: new contents for file')
        self.repo.commit('edit note file')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f1],
             },
            results,
        )

    def test_legacy_file(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file('slug1', legacy=True)
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        f2 = f1.replace('slug1', 'slug2')
        self.repo.git('mv', f1, f2)
        self.repo.commit('rename note file')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f2],
             },
            results,
        )

    def test_rename_legacy_file_to_new(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file('slug1', legacy=True)
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        # Rename the file with the new convention of placing the UUID
        # after the slug instead of before.
        f2 = f1.replace('0000000000000001-slug1',
                        'slug1-0000000000000001')
        self.repo.git('mv', f1, f2)
        self.repo.commit('rename note file')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f2],
             },
            results,
        )

    def test_limit_by_earliest_version(self):
        self._make_python_package()
        self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f2 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'middle tag', '2.0.0')
        f3 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'last tag', '3.0.0')
        self.c.override(
            earliest_version='2.0.0',
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f2],
             '3.0.0': [f3],
             },
            results,
        )

    def test_delete_file(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file('slug1')
        f2 = self._add_notes_file('slug2')
        self.repo.git('rm', f1)
        self.repo.commit('remove note file')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f2],
             },
            results,
        )

    def test_rename_then_delete_file(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        f1 = self._add_notes_file('slug1')
        f2 = f1.replace('slug1', 'slug2')
        self.repo.git('mv', f1, f2)
        self.repo.git('status')
        self.repo.commit('rename note file')
        self.repo.git('rm', f2)
        self.repo.commit('remove note file')
        f3 = self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        log_results = self.repo.git('log', '--topo-order',
                                    '--pretty=%H %d',
                                    '--name-only')
        self.addDetail('git log', text_content(log_results))
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'2.0.0': [f3],
             },
            results,
        )


class FileContentsTest(Base):

    def test_basic_file(self):
        # Prove that we can get a file we have committed.
        f1 = self._add_notes_file(contents='well-known-contents')
        r = scanner.RenoRepo(self.reporoot)
        contents = r.get_file_at_commit(f1, 'HEAD')
        self.assertEqual(
            b'well-known-contents',
            contents,
        )

    def test_no_such_file(self):
        # Returns None when the file does not exist at all.
        # (we have to commit something, otherwise there is no HEAD)
        self._add_notes_file(contents='well-known-contents')
        r = scanner.RenoRepo(self.reporoot)
        contents = r.get_file_at_commit('no-such-dir/no-such-file', 'HEAD')
        self.assertEqual(
            None,
            contents,
        )

    def test_edit_file_and_commit(self):
        # Prove that we can edit a file and see the changes.
        f1 = self._add_notes_file(contents='initial-contents')
        with open(os.path.join(self.reporoot, f1), 'w') as f:
            f.write('new contents for file')
        self.repo.commit('edit note file')
        r = scanner.RenoRepo(self.reporoot)
        contents = r.get_file_at_commit(f1, 'HEAD')
        self.assertEqual(
            b'new contents for file',
            contents,
        )

    def test_earlier_version_of_edited_file(self):
        # Prove that we are not always just returning the most current
        # version of a file.
        f1 = self._add_notes_file(contents='initial-contents')
        with open(os.path.join(self.reporoot, f1), 'w') as f:
            f.write('new contents for file')
        self.repo.commit('edit note file')
        self.scanner = scanner.Scanner(self.c)
        r = scanner.RenoRepo(self.reporoot)
        head = r.head()
        parent = r.get_parents(head)[0]
        parent = parent.decode('ascii')
        contents = r.get_file_at_commit(f1, parent)
        self.assertEqual(
            b'initial-contents',
            contents,
        )

    def test_edit_file_without_commit(self):
        # Prove we are not picking up the contents from the local
        # filesystem outside of the git history.
        f1 = self._add_notes_file(contents='initial-contents')
        with open(os.path.join(self.reporoot, f1), 'w') as f:
            f.write('new contents for file')
        r = scanner.RenoRepo(self.reporoot)
        contents = r.get_file_at_commit(f1, 'HEAD')
        self.assertEqual(
            b'initial-contents',
            contents,
        )


class PreReleaseTest(Base):

    def test_alpha(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0.0a1')
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0.0a2')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0.0a2': [f1],
             },
            results,
        )

    def test_beta(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0.0b1')
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0.0b2')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0.0b2': [f1],
             },
            results,
        )

    def test_release_candidate(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0.0rc1')
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0.0rc2')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0.0rc2': [f1],
             },
            results,
        )

    def test_collapse(self):
        files = []
        self._make_python_package()
        files.append(self._add_notes_file('slug1'))
        self.repo.git('tag', '-s', '-m', 'alpha tag', '1.0.0.0a1')
        files.append(self._add_notes_file('slug2'))
        self.repo.git('tag', '-s', '-m', 'beta tag', '1.0.0.0b1')
        files.append(self._add_notes_file('slug3'))
        self.repo.git('tag', '-s', '-m', 'release candidate tag', '1.0.0.0rc1')
        files.append(self._add_notes_file('slug4'))
        self.repo.git('tag', '-s', '-m', 'full release tag', '1.0.0')
        self.c.override(
            collapse_pre_releases=True,
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': files,
             },
            results,
        )

    def test_collapse_without_full_release(self):
        self._make_python_package()
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'alpha tag', '1.0.0.0a1')
        f2 = self._add_notes_file('slug2')
        self.repo.git('tag', '-s', '-m', 'beta tag', '1.0.0.0b1')
        f3 = self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'release candidate tag', '1.0.0.0rc1')
        self.c.override(
            collapse_pre_releases=True,
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0.0a1': [f1],
             '1.0.0.0b1': [f2],
             '1.0.0.0rc1': [f3],
             },
            results,
        )

    def test_collapse_without_notes(self):
        self._make_python_package()
        self.repo.git('tag', '-s', '-m', 'earlier tag', '0.1.0')
        f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'alpha tag', '1.0.0.0a1')
        f2 = self._add_notes_file('slug2')
        self.repo.git('tag', '-s', '-m', 'beta tag', '1.0.0.0b1')
        f3 = self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'release candidate tag', '1.0.0.0rc1')
        self.c.override(
            collapse_pre_releases=True,
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0.0a1': [f1],
             '1.0.0.0b1': [f2],
             '1.0.0.0rc1': [f3],
             },
            results,
        )


class MergeCommitTest(Base):

    def test_1(self):
        # Create changes on master and in the branch
        # in order so the history is "normal"
        n1 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.repo.git('checkout', '-b', 'test_merge_commit')
        n2 = self._add_notes_file()
        self.repo.git('checkout', 'master')
        self.repo.add_file('ignore-1.txt')
        # Merge the branch into master.
        self.repo.git('merge', '--no-ff', 'test_merge_commit')
        time.sleep(0.1)  # force a delay between commits
        self.repo.add_file('ignore-2.txt')
        self.repo.git('tag', '-s', '-m', 'second tag', '2.0.0')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [n1],
             '2.0.0': [n2]},
            results,
        )
        self.assertEqual(
            ['2.0.0', '1.0.0'],
            list(raw_results.keys()),
        )

    def test_2(self):
        # Create changes on the branch before the tag into which it is
        # actually merged.
        self.repo.add_file('ignore-0.txt')
        self.repo.git('checkout', '-b', 'test_merge_commit')
        n1 = self._add_notes_file()
        self.repo.git('checkout', 'master')
        n2 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.repo.add_file('ignore-1.txt')
        # Merge the branch into master.
        self.repo.git('merge', '--no-ff', 'test_merge_commit')
        time.sleep(0.1)  # force a delay between commits
        self.repo.git('show')
        self.repo.add_file('ignore-2.txt')
        self.repo.git('tag', '-s', '-m', 'second tag', '2.0.0')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [n2],
             '2.0.0': [n1]},
            results,
        )
        self.assertEqual(
            ['2.0.0', '1.0.0'],
            list(raw_results.keys()),
        )

    def test_3(self):
        # Create changes on the branch before the tag into which it is
        # actually merged, with another tag in between the time of the
        # commit and the time of the merge. This should reflect the
        # order of events described in bug #1522153.
        self.repo.add_file('ignore-0.txt')
        self.repo.git('checkout', '-b', 'test_merge_commit')
        n1 = self._add_notes_file()
        self.repo.git('checkout', 'master')
        n2 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.repo.add_file('ignore-1.txt')
        self.repo.git('tag', '-s', '-m', 'second tag', '1.1.0')
        self.repo.git('merge', '--no-ff', 'test_merge_commit')
        time.sleep(0.1)  # force a delay between commits
        self.repo.add_file('ignore-2.txt')
        self.repo.git('tag', '-s', '-m', 'third tag', '2.0.0')
        self.repo.add_file('ignore-3.txt')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        # Since the 1.1.0 tag has no notes files, it does not appear
        # in the output. It's only there to trigger the bug as it was
        # originally reported.
        self.assertEqual(
            {'1.0.0': [n2],
             '2.0.0': [n1]},
            results,
        )
        self.assertEqual(
            ['2.0.0', '1.0.0'],
            list(raw_results.keys()),
        )

    def test_4(self):
        # Create changes on the branch before the tag into which it is
        # actually merged, with another tag in between the time of the
        # commit and the time of the merge. This should reflect the
        # order of events described in bug #1522153.
        self.repo.add_file('ignore-0.txt')
        self.repo.git('checkout', '-b', 'test_merge_commit')
        n1 = self._add_notes_file()
        self.repo.git('checkout', 'master')
        n2 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.repo.add_file('ignore-1.txt')
        n3 = self._add_notes_file()
        self.repo.git('tag', '-s', '-m', 'second tag', '1.1.0')
        self.repo.git('merge', '--no-ff', 'test_merge_commit')
        time.sleep(0.1)  # force a delay between commits
        self.repo.add_file('ignore-2.txt')
        self.repo.git('tag', '-s', '-m', 'third tag', '2.0.0')
        self.repo.add_file('ignore-3.txt')
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {'1.0.0': [n2],
             '1.1.0': [n3],
             '2.0.0': [n1]},
            results,
        )
        self.assertEqual(
            ['2.0.0', '1.1.0', '1.0.0'],
            list(raw_results.keys()),
        )


class UniqueIdTest(Base):

    def test_legacy(self):
        uid = scanner._get_unique_id(
            'releasenotes/notes/0000000000000001-slug1.yaml'
        )
        self.assertEqual('0000000000000001', uid)

    def test_modern(self):
        uid = scanner._get_unique_id(
            'releasenotes/notes/slug1-0000000000000001.yaml'
        )
        self.assertEqual('0000000000000001', uid)


class BranchBaseTest(Base):

    def setUp(self):
        super(BranchBaseTest, self).setUp()
        self._make_python_package()
        self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self._add_notes_file('slug2')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'first tag', '3.0.0')
        self.repo.git('checkout', '2.0.0')
        self.repo.git('branch', 'not-master')
        self.repo.git('checkout', 'master')
        self.scanner = scanner.Scanner(self.c)

    def test_current_branch_no_extra_commits(self):
        # checkout the branch and then ask for its base
        self.repo.git('checkout', 'not-master')
        self.assertEqual(
            '2.0.0',
            self.scanner._get_branch_base('not-master'),
        )

    def test_current_branch_extra_commit(self):
        # checkout the branch and then ask for its base
        self.repo.git('checkout', 'not-master')
        self._add_notes_file('slug4')
        self.assertEqual(
            '2.0.0',
            self.scanner._get_branch_base('not-master'),
        )

    def test_alternate_branch_no_extra_commits(self):
        # checkout master and then ask for the alternate branch base
        self.repo.git('checkout', 'master')
        self.assertEqual(
            '2.0.0',
            self.scanner._get_branch_base('not-master'),
        )

    def test_alternate_branch_extra_commit(self):
        # checkout master and then ask for the alternate branch base
        self.repo.git('checkout', 'not-master')
        self._add_notes_file('slug4')
        self.repo.git('checkout', 'master')
        self.assertEqual(
            '2.0.0',
            self.scanner._get_branch_base('not-master'),
        )


class BranchTest(Base):

    def setUp(self):
        super(BranchTest, self).setUp()
        self._make_python_package()
        self.f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.f2 = self._add_notes_file('slug2')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'first tag', '3.0.0')

    def test_files_current_branch(self):
        self.repo.git('checkout', '2.0.0')
        self.repo.git('checkout', '-b', 'stable/2')
        f21 = self._add_notes_file('slug21')
        log_text = self.repo.git('log', '--decorate')
        self.addDetail('git log', text_content(log_text))
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '1.0.0': [self.f1],
                '2.0.0': [self.f2],
                '2.0.0-1': [f21],
            },
            results,
        )

    def test_files_stable_from_master(self):
        self.repo.git('checkout', '2.0.0')
        self.repo.git('checkout', '-b', 'stable/2')
        f21 = self._add_notes_file('slug21')
        self.repo.git('checkout', 'master')
        log_text = self.repo.git('log', '--pretty=%x00%H %d', '--name-only',
                                 'stable/2')
        self.addDetail('git log', text_content(log_text))
        self.c.override(
            branch='stable/2',
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '2.0.0': [self.f2],
                '2.0.0-1': [f21],
            },
            results,
        )

    def test_files_stable_from_master_no_stop_base(self):
        self.repo.git('checkout', '2.0.0')
        self.repo.git('checkout', '-b', 'stable/2')
        f21 = self._add_notes_file('slug21')
        self.repo.git('checkout', 'master')
        log_text = self.repo.git('log', '--pretty=%x00%H %d', '--name-only',
                                 'stable/2')
        self.addDetail('git log', text_content(log_text))
        self.c.override(
            branch='stable/2',
        )
        self.c.override(
            stop_at_branch_base=False,
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '1.0.0': [self.f1],
                '2.0.0': [self.f2],
                '2.0.0-1': [f21],
            },
            results,
        )

    def test_pre_release_branch_no_collapse(self):
        f4 = self._add_notes_file('slug4')
        self.repo.git('tag', '-s', '-m', 'pre-release', '4.0.0.0rc1')
        # Add a commit on master after the tag
        self._add_notes_file('slug5')
        # Move back to the tag and create the branch
        self.repo.git('checkout', '4.0.0.0rc1')
        self.repo.git('checkout', '-b', 'stable/4')
        # Create a commit on the branch
        f41 = self._add_notes_file('slug41')
        log_text = self.repo.git(
            'log', '--pretty=%x00%H %d', '--name-only', '--graph',
            '--all', '--decorate',
        )
        self.addDetail('git log', text_content(log_text))
        rev_list = self.repo.git('rev-list', '--first-parent',
                                 '^stable/4', 'master')
        self.addDetail('rev-list', text_content(rev_list))
        self.c.override(
            branch='stable/4',
            collapse_pre_releases=False,
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '4.0.0.0rc1': [f4],
                '4.0.0.0rc1-1': [f41],
            },
            results,
        )

    def test_pre_release_branch_collapse(self):
        f4 = self._add_notes_file('slug4')
        self.repo.git('tag', '-s', '-m', 'pre-release', '4.0.0.0rc1')
        # Add a commit on master after the tag
        self._add_notes_file('slug5')
        # Move back to the tag and create the branch
        self.repo.git('checkout', '4.0.0.0rc1')
        self.repo.git('checkout', '-b', 'stable/4')
        # Create a commit on the branch
        f41 = self._add_notes_file('slug41')
        self.repo.git('tag', '-s', '-m', 'release', '4.0.0')
        log_text = self.repo.git(
            'log', '--pretty=%x00%H %d', '--name-only', '--graph',
            '--all', '--decorate',
        )
        self.addDetail('git log', text_content(log_text))
        rev_list = self.repo.git('rev-list', '--first-parent',
                                 '^stable/4', 'master')
        self.addDetail('rev-list', text_content(rev_list))
        self.c.override(
            branch='stable/4',
            collapse_pre_releases=True,
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '4.0.0': [f4, f41],
            },
            results,
        )

    def test_full_release_branch(self):
        f4 = self._add_notes_file('slug4')
        self.repo.git('tag', '-s', '-m', 'release', '4.0.0')
        # Add a commit on master after the tag
        self._add_notes_file('slug5')
        # Move back to the tag and create the branch
        self.repo.git('checkout', '4.0.0')
        self.repo.git('checkout', '-b', 'stable/4')
        # Create a commit on the branch
        f41 = self._add_notes_file('slug41')
        log_text = self.repo.git(
            'log', '--pretty=%x00%H %d', '--name-only', '--graph',
            '--all', '--decorate',
        )
        self.addDetail('git log', text_content(log_text))
        rev_list = self.repo.git('rev-list', '--first-parent',
                                 '^stable/4', 'master')
        self.addDetail('rev-list', text_content(rev_list))
        self.c.override(
            branch='stable/4',
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '4.0.0': [f4],
                '4.0.0-1': [f41],
            },
            results,
        )

    def test_branch_tip_of_master(self):
        # We have branched from master, but not added any commits to
        # master.
        f4 = self._add_notes_file('slug4')
        self.repo.git('tag', '-s', '-m', 'release', '4.0.0')
        self.repo.git('checkout', '-b', 'stable/4')
        # Create a commit on the branch
        f41 = self._add_notes_file('slug41')
        f42 = self._add_notes_file('slug42')
        log_text = self.repo.git(
            'log', '--pretty=%x00%H %d', '--name-only', '--graph',
            '--all', '--decorate',
        )
        self.addDetail('git log', text_content(log_text))
        rev_list = self.repo.git('rev-list', '--first-parent',
                                 '^stable/4', 'master')
        self.addDetail('rev-list', text_content(rev_list))
        self.c.override(
            branch='stable/4',
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '4.0.0': [f4],
                '4.0.0-2': [f41, f42],
            },
            results,
        )

    def test_branch_no_more_commits(self):
        # We have branched from master, but not added any commits to
        # our branch or to master.
        f4 = self._add_notes_file('slug4')
        self.repo.git('tag', '-s', '-m', 'release', '4.0.0')
        self.repo.git('checkout', '-b', 'stable/4')
        # Create a commit on the branch
        log_text = self.repo.git(
            'log', '--pretty=%x00%H %d', '--name-only', '--graph',
            '--all', '--decorate',
        )
        self.addDetail('git log', text_content(log_text))
        rev_list = self.repo.git('rev-list', '--first-parent',
                                 '^stable/4', 'master')
        self.addDetail('rev-list', text_content(rev_list))
        self.c.override(
            branch='stable/4',
        )
        self.scanner = scanner.Scanner(self.c)
        raw_results = self.scanner.get_notes_by_version()
        results = {
            k: [f for (f, n) in v]
            for (k, v) in raw_results.items()
        }
        self.assertEqual(
            {
                '4.0.0': [f4],
            },
            results,
        )

    def test_remote_branches(self):
        self.repo.git('checkout', '2.0.0')
        self.repo.git('checkout', '-b', 'stable/2')
        self.repo.git('checkout', 'master')
        scanner1 = scanner.Scanner(self.c)
        head1 = scanner1._get_ref('stable/2')
        self.assertIsNotNone(head1)
        print('head1', head1)
        # Create a second repository by cloning the first.
        print(utils.check_output(
            ['git', 'clone', self.reporoot, 'reporoot2'],
            cwd=self.temp_dir,
        ))
        reporoot2 = os.path.join(self.temp_dir, 'reporoot2')
        print(utils.check_output(
            ['git', 'remote', 'update'],
            cwd=reporoot2,
        ))
        print(utils.check_output(
            ['git', 'remote', '-v'],
            cwd=reporoot2,
        ))
        print(utils.check_output(
            ['find', '.git/refs'],
            cwd=reporoot2,
        ))
        print(utils.check_output(
            ['git', 'branch', '-a'],
            cwd=reporoot2,
        ))
        c2 = config.Config(reporoot2)
        scanner2 = scanner.Scanner(c2)
        head2 = scanner2._get_ref('origin/stable/2')
        self.assertIsNotNone(head2)
        self.assertEqual(head1, head2)


class TagsTest(Base):

    def setUp(self):
        super(TagsTest, self).setUp()
        self._make_python_package()
        self.f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.f2 = self._add_notes_file('slug2')
        self.repo.git('tag', '-s', '-m', 'first tag', '2.0.0')
        self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'first tag', '3.0.0')

    def test_master(self):
        self.scanner = scanner.Scanner(self.c)
        results = self.scanner._get_tags_on_branch(None)
        self.assertEqual(
            ['3.0.0', '2.0.0', '1.0.0'],
            results,
        )

    def test_get_ref(self):
        self.scanner = scanner.Scanner(self.c)
        ref = self.scanner._get_ref('3.0.0')
        expected = self.scanner._repo.head()
        self.assertEqual(expected, ref)

    def test_not_master(self):
        self.repo.git('checkout', '2.0.0')
        self.repo.git('checkout', '-b', 'not-master')
        self._add_notes_file('slug4')
        self.repo.git('tag', '-s', '-m', 'not on master', '2.0.1')
        self.repo.git('checkout', 'master')
        self.scanner = scanner.Scanner(self.c)
        results = self.scanner._get_tags_on_branch('not-master')
        self.assertEqual(
            ['2.0.1', '2.0.0', '1.0.0'],
            results,
        )

    def test_unsigned(self):
        self._add_notes_file('slug4')
        self.repo.git('tag', '-m', 'first tag', '4.0.0')
        self.scanner = scanner.Scanner(self.c)
        results = self.scanner._get_tags_on_branch(None)
        self.assertEqual(
            ['4.0.0', '3.0.0', '2.0.0', '1.0.0'],
            results,
        )


class VersionTest(Base):

    def setUp(self):
        super(VersionTest, self).setUp()
        self._make_python_package()
        self.f1 = self._add_notes_file('slug1')
        self.repo.git('tag', '-s', '-m', 'first tag', '1.0.0')
        self.f2 = self._add_notes_file('slug2')
        self.repo.git('tag', '-s', '-m', 'second tag', '2.0.0')
        self._add_notes_file('slug3')
        self.repo.git('tag', '-s', '-m', 'third tag', '3.0.0')

    def test_tagged_head(self):
        self.scanner = scanner.Scanner(self.c)
        results = self.scanner._get_current_version(None)
        self.assertEqual(
            '3.0.0',
            results,
        )

    def test_head_after_tag(self):
        self._add_notes_file('slug4')
        self.scanner = scanner.Scanner(self.c)
        results = self.scanner._get_current_version(None)
        self.assertEqual(
            '3.0.0-1',
            results,
        )

    def test_multiple_tags(self):
        # The timestamp resolution appears to be 1 second, so sleep to
        # ensure distinct timestamps for the 2 tags. In practice it is
        # unlikely that anything could apply 2 signed tags within a
        # single second (certainly not a person).
        time.sleep(1)
        self.repo.git('tag', '-s', '-m', 'fourth tag', '4.0.0')
        self.scanner = scanner.Scanner(self.c)
        results = self.scanner._get_current_version(None)
        self.assertEqual(
            '4.0.0',
            results,
        )


class AggregateChangesTest(Base):

    def test_ignore(self):
        entry = mock.Mock()
        n = self.get_note_num()
        name = 'prefix/add-%016x' % n  # no .yaml extension
        entry.commit.id = 'commit-id'
        changes = [
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_ADD,
                old=objects.TreeEntry(path=None, mode=None, sha=None),
                new=objects.TreeEntry(
                    path=name.encode('utf-8'),
                    mode='0222',
                    sha='not-a-hash',
                )
            )
        ]
        results = scanner._aggregate_changes(entry, changes, 'prefix')
        self.assertEqual(
            [],
            results,
        )

    def test_add(self):
        entry = mock.Mock()
        n = self.get_note_num()
        name = 'prefix/add-%016x.yaml' % n
        entry.commit.id = 'commit-id'
        changes = [
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_ADD,
                old=objects.TreeEntry(path=None, mode=None, sha=None),
                new=objects.TreeEntry(
                    path=name.encode('utf-8'),
                    mode='0222',
                    sha='not-a-hash',
                )
            )
        ]
        results = list(scanner._aggregate_changes(entry, changes, 'prefix'))
        self.assertEqual(
            [('%016x' % n, 'add', name, 'commit-id')],
            results,
        )

    def test_delete(self):
        entry = mock.Mock()
        n = self.get_note_num()
        name = 'prefix/delete-%016x.yaml' % n
        entry.commit.id = 'commit-id'
        changes = [
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_DELETE,
                old=objects.TreeEntry(
                    path=name.encode('utf-8'),
                    mode='0222',
                    sha='not-a-hash',
                ),
                new=objects.TreeEntry(path=None, mode=None, sha=None)
            )
        ]
        results = list(scanner._aggregate_changes(entry, changes, 'prefix'))
        self.assertEqual(
            [('%016x' % n, 'delete', name)],
            results,
        )

    def test_change(self):
        entry = mock.Mock()
        n = self.get_note_num()
        name = 'prefix/change-%016x.yaml' % n
        entry.commit.id = 'commit-id'
        changes = [
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_MODIFY,
                old=objects.TreeEntry(
                    path=name.encode('utf-8'),
                    mode='0222',
                    sha='old-sha',
                ),
                new=objects.TreeEntry(
                    path=name.encode('utf-8'),
                    mode='0222',
                    sha='new-sha',
                ),
            )
        ]
        results = list(scanner._aggregate_changes(entry, changes, 'prefix'))
        self.assertEqual(
            [('%016x' % n, 'modify', name, 'commit-id')],
            results,
        )

    def test_add_then_delete(self):
        entry = mock.Mock()
        n = self.get_note_num()
        new_name = 'prefix/new-%016x.yaml' % n
        old_name = 'prefix/old-%016x.yaml' % n
        entry.commit.id = 'commit-id'
        changes = [
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_ADD,
                old=objects.TreeEntry(path=None, mode=None, sha=None),
                new=objects.TreeEntry(
                    path=new_name.encode('utf-8'),
                    mode='0222',
                    sha='new-hash',
                )
            ),
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_DELETE,
                old=objects.TreeEntry(
                    path=old_name.encode('utf-8'),
                    mode='0222',
                    sha='old-hash',
                ),
                new=objects.TreeEntry(path=None, mode=None, sha=None)
            )
        ]
        results = list(scanner._aggregate_changes(entry, changes, 'prefix'))
        self.assertEqual(
            [('%016x' % n, 'rename', old_name, new_name, 'commit-id')],
            results,
        )

    def test_delete_then_add(self):
        entry = mock.Mock()
        n = self.get_note_num()
        new_name = 'prefix/new-%016x.yaml' % n
        old_name = 'prefix/old-%016x.yaml' % n
        entry.commit.id = 'commit-id'
        changes = [
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_DELETE,
                old=objects.TreeEntry(
                    path=old_name.encode('utf-8'),
                    mode='0222',
                    sha='old-hash',
                ),
                new=objects.TreeEntry(path=None, mode=None, sha=None)
            ),
            diff_tree.TreeChange(
                type=diff_tree.CHANGE_ADD,
                old=objects.TreeEntry(path=None, mode=None, sha=None),
                new=objects.TreeEntry(
                    path=new_name.encode('utf-8'),
                    mode='0222',
                    sha='new-hash',
                )
            ),
        ]
        results = list(scanner._aggregate_changes(entry, changes, 'prefix'))
        self.assertEqual(
            [('%016x' % n, 'rename', old_name, new_name, 'commit-id')],
            results,
        )

    def test_tree_changes(self):
        # Under some conditions when dulwich sees merge commits,
        # changes() returns a list with nested lists. See commit
        # cc11da6dcfb1dbaa015e9804b6a23f7872380c1b in this repo for an
        # example.
        entry = mock.Mock()
        n = self.get_note_num()
        # The files modified by the commit are actually
        # reno/scanner.py, but the fake names are used in this test to
        # comply with the rest of the configuration for the scanner.
        old_name = 'prefix/old-%016x.yaml' % n
        entry.commit.id = 'commit-id'
        changes = [[
            diff_tree.TreeChange(
                type='modify',
                old=diff_tree.TreeEntry(
                    path=old_name.encode('utf-8'),
                    mode=33188,
                    sha=b'8247dfdd116fd0e3cc4ba32328e4a3eafd227de6',
                ),
                new=diff_tree.TreeEntry(
                    path=old_name.encode('utf-8'),
                    mode=33188,
                    sha=b'611f3663f54afb1f018a6a8680b6488da50ac340',
                ),
            ),
            diff_tree.TreeChange(
                type='modify',
                old=diff_tree.TreeEntry(
                    path=old_name.encode('utf-8'),
                    mode=33188,
                    sha=b'ecb7788066eefa9dc8f110b56360efe7b1140b84',
                ),
                new=diff_tree.TreeEntry(
                    path=old_name.encode('utf-8'),
                    mode=33188,
                    sha=b'611f3663f54afb1f018a6a8680b6488da50ac340',
                ),
            ),
        ]]
        results = list(scanner._aggregate_changes(entry, changes, 'prefix'))
        self.assertEqual(
            [('%016x' % n, 'modify', old_name, 'commit-id'),
             ('%016x' % n, 'modify', old_name, 'commit-id')],
            results,
        )
