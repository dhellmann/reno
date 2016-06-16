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

import textwrap

from reno import cache
from reno.tests import base

from oslotest import mockpatch

import mock


class TestCache(base.TestCase):

    scanner_output = {
        '0.0.0': [('note1', 'shaA')],
        '1.0.0': [('note2', 'shaB'), ('note3', 'shaC')],
    }

    note_bodies = {
        'note1': textwrap.dedent("""
        prelude: >
          This is the prelude.
        """),
        'note2': textwrap.dedent("""
        issues:
          - This is the first issue.
          - This is the second issue.
        """),
        'note3': textwrap.dedent("""
        features:
          - We added a feature!
        """)
    }

    def _get_note_body(self, reporoot, filename, sha):
        return self.note_bodies.get(filename, '')

    def setUp(self):
        super(TestCache, self).setUp()
        self.useFixture(
            mockpatch.Patch('reno.scanner.get_file_at_commit',
                            new=self._get_note_body)
        )

    def test_build_cache_db(self):
        with mock.patch('reno.scanner.get_notes_by_version') as gnbv:
            gnbv.return_value = self.scanner_output
            db = cache.build_cache_db(
                reporoot=None,
                notesdir=None,
                branch=None,
                collapse_pre_releases=True,
                versions_to_include=[],
                earliest_version=None,
            )
            expected = {
                'notes': [
                    {'version': k, 'files': v}
                    for k, v in self.scanner_output.items()
                ],
                'file-contents': {
                    'note1': {
                        'prelude': 'This is the prelude.\n',
                    },
                    'note2': {
                        'issues': [
                            'This is the first issue.',
                            'This is the second issue.',
                        ],
                    },
                    'note3': {
                        'features': ['We added a feature!'],
                    },
                },
            }
            self.assertEqual(expected, db)