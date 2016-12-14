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

from __future__ import print_function

import collections
import fnmatch
import logging
import os.path
import re
import sys

from dulwich import diff_tree
from dulwich import objects
from dulwich import repo

LOG = logging.getLogger(__name__)

# What does a pre-release version number look like?
PRE_RELEASE_RE = re.compile('''
    \.(\d+(?:[ab]|rc)+\d*)$
''', flags=re.VERBOSE | re.UNICODE)


def _get_unique_id(filename):
    base = os.path.basename(filename)
    root, ext = os.path.splitext(base)
    uniqueid = root[-16:]
    if '-' in uniqueid:
        # This is an older file with the UUID at the beginning
        # of the name.
        uniqueid = root[:16]
    return uniqueid


def _note_file(name):
    """Return bool indicating if the filename looks like a note file.

    This is used to filter the files in changes based on the notes
    directory we were given. We cannot do this in the walker directly
    because it means we end up skipping some of the tags if the
    commits being tagged don't include any release note files.

    """
    if not name:
        return False
    if fnmatch.fnmatch(name, '*.yaml'):
        return True
    else:
        LOG.warning('found and ignored extra file %s', name)
    return False


def _changes_in_subdir(repo, walk_entry, subdir):
    """Iterator producing changes of interest to reno.

    The default changes() method of a WalkEntry computes all of the
    changes in the entire repo at that point. We only care about
    changes in a subdirectory, so this reimplements
    WalkeEntry.changes() with that filter in place.

    The alternative, passing paths to the TreeWalker, does not work
    because we need all of the commits in sequence so we can tell when
    the tag changes. We have to look at every commit to see if it
    either has a tag, a note file, or both.

    NOTE(dhellmann): The TreeChange entries returned as a result of
    the manipulation done by this function have the subdir prefix
    stripped.

    """
    commit = walk_entry.commit
    store = repo.object_store

    parents = walk_entry._get_parents(commit)

    if not parents:
        changes_func = diff_tree.tree_changes
        parent_subtree = None
    elif len(parents) == 1:
        changes_func = diff_tree.tree_changes
        parent_tree = repo[repo[parents[0]].tree]
        parent_subtree = repo._get_subtree(parent_tree, subdir)
        if parent_subtree:
            parent_subtree = parent_subtree.sha().hexdigest().encode('ascii')
    else:
        changes_func = diff_tree.tree_changes_for_merge
        parent_subtree = [
            repo._get_subtree(repo[repo[p].tree], subdir)
            for p in parents
        ]
        parent_subtree = [
            p.sha().hexdigest().encode('ascii')
            for p in parent_subtree
            if p
        ]
    subdir_tree = repo._get_subtree(repo[commit.tree], subdir)
    if subdir_tree:
        commit_subtree = subdir_tree.sha().hexdigest().encode('ascii')
    else:
        commit_subtree = None
    if parent_subtree == commit_subtree:
        return []
    return changes_func(store, parent_subtree, commit_subtree)


def _aggregate_changes(walk_entry, changes, notesdir):
    """Collapse a series of changes based on uniqueness for file uids.

    The list of TreeChange instances describe changes between the old
    and new repository trees. The change has a type, and new and old
    paths and shas.

    Simple add, delete, and change operations are handled directly.

    There is a rename type, but detection of renamed files is
    incomplete so we handle that ourselves based on the UID value
    built into the filenames (under the assumption that if someone
    changes that part of the filename they want it treated as a
    different file for some reason).  If we see both an add and a
    delete for a given UID treat that as a rename.

    The SHA values returned are for the commit, rather than the blob
    values in the TreeChange objects.

    The path values in the change entries are encoded, so we decode
    them to compare them against the notesdir and file pattern in
    _note_file() and then return the decoded values to make consuming
    them easier.

    """
    sha = walk_entry.commit.id
    by_uid = collections.defaultdict(list)
    for ec in changes:
        if not isinstance(ec, list):
            ec = [ec]
        else:
            ec = ec
        for c in ec:
            LOG.debug('change %r', c)
            if c.type == diff_tree.CHANGE_ADD:
                path = c.new.path.decode('utf-8') if c.new.path else None
                if _note_file(path):
                    uid = _get_unique_id(path)
                    by_uid[uid].append((c.type, path, sha))
                else:
                    LOG.debug('ignoring')
            elif c.type == diff_tree.CHANGE_DELETE:
                path = c.old.path.decode('utf-8') if c.old.path else None
                if _note_file(path):
                    uid = _get_unique_id(path)
                    by_uid[uid].append((c.type, path))
                else:
                    LOG.debug('ignoring')
            elif c.type == diff_tree.CHANGE_MODIFY:
                path = c.new.path.decode('utf-8') if c.new.path else None
                if _note_file(path):
                    uid = _get_unique_id(path)
                    by_uid[uid].append((c.type, path, sha))
                else:
                    LOG.debug('ignoring')
            else:
                raise ValueError('unhandled change type: {!r}'.format(c))

    results = []
    for uid, changes in sorted(by_uid.items()):
        if len(changes) == 1:
            results.append((uid,) + changes[0])
        else:
            types = set(c[0] for c in changes)
            if types == set([diff_tree.CHANGE_ADD, diff_tree.CHANGE_DELETE]):
                # A rename, combine the data from the add and delete entries.
                added = [
                    c for c in changes if c[0] == diff_tree.CHANGE_ADD
                ][0]
                deled = [
                    c for c in changes if c[0] == diff_tree.CHANGE_DELETE
                ][0]
                results.append(
                    (uid, diff_tree.CHANGE_RENAME, deled[1]) + added[1:]
                )
            elif types == set([diff_tree.CHANGE_MODIFY]):
                # Merge commit with modifications to the same files in
                # different commits.
                for c in changes:
                    results.append((uid, diff_tree.CHANGE_MODIFY, c[1], sha))
            else:
                raise ValueError('Unrecognized changes: {!r}'.format(changes))
    return results


class RenoRepo(repo.Repo):

    # Populated by _load_tags().
    _all_tags = None
    _shas_to_tags = None

    def _load_tags(self):
        self._all_tags = {
            k.partition(b'/tags/')[-1].decode('utf-8'): v
            for k, v in self.get_refs().items()
            if k.startswith(b'refs/tags/')
        }
        self._shas_to_tags = {}
        for tag, tag_sha in self._all_tags.items():
            tag_obj = self[tag_sha]
            if isinstance(tag_obj, objects.Tag):
                # A signed tag has its own SHA, but the tag refers to
                # the commit and that's the SHA we'll see when we scan
                # commits on a branch.
                tagged_sha = tag_obj.object[1]
                date = tag_obj.tag_time
            elif isinstance(tag_obj, objects.Commit):
                # Unsigned tags refer directly to commits. This seems
                # to especially happen when the tag definition moves
                # to the packed-refs list instead of being represented
                # by its own file.
                tagged_sha = tag_obj.id
                date = tag_obj.commit_time
            else:
                raise ValueError(
                    ('Unrecognized tag object {!r} with '
                     'tag {} and SHA {!r}: {}').format(
                        tag_obj, tag, tag_sha, type(tag_obj))
                )
            self._shas_to_tags.setdefault(tagged_sha, []).append((tag, date))

    def get_tags_on_commit(self, sha):
        "Return the tag(s) on a commit, in application order."
        if self._all_tags is None:
            self._load_tags()
        tags_and_dates = self._shas_to_tags.get(sha, [])
        tags_and_dates.sort(key=lambda x: x[1])
        return [t[0] for t in tags_and_dates]

    def _get_subtree(self, tree, path):
        "Given a tree SHA and a path, return the SHA of the subtree."
        try:
            if os.sep in path:
                # The tree entry will only have a single level of the
                # directory name, so if we have a / in our filename we
                # know we're going to have to keep traversing the
                # tree.
                prefix, _, trailing = path.partition(os.sep)
                mode, subtree_sha = tree[prefix.encode('utf-8')]
                subtree = self[subtree_sha]
                return self._get_subtree(subtree, trailing)
            else:
                # The tree entry will point to the SHA of the contents
                # of the subtree.
                mode, sha = tree[path.encode('utf-8')]
                result = self[sha]
                return result
        except KeyError:
            # Some part of the path wasn't found, so the subtree is
            # not present. Return the sentinel value.
            return None

    def _get_file_from_tree(self, filename, tree):
        "Given a tree object, traverse it to find the file."
        try:
            if os.sep in filename:
                # The tree entry will only have a single level of the
                # directory name, so if we have a / in our filename we
                # know we're going to have to keep traversing the
                # tree.
                prefix, _, trailing = filename.partition(os.sep)
                mode, subtree_sha = tree[prefix.encode('utf-8')]
                subtree = self[subtree_sha]
                return self._get_file_from_tree(trailing, subtree)
            else:
                # The tree entry will point to the blob with the
                # contents of the file.
                mode, file_blob_sha = tree[filename.encode('utf-8')]
                file_blob = self[file_blob_sha]
                return file_blob.data
        except KeyError:
            # Some part of the filename wasn't found, so the file is
            # not present. Return the sentinel value.
            return None

    def get_file_at_commit(self, filename, sha):
        "Return the contents of the file if it exists at the commit, or None."
        # Get the tree associated with the commit identified by the
        # input SHA, then look through the items in the tree to find
        # the one with the path matching the filename. Take the
        # associated SHA from the tree and get the file contents from
        # the repository.
        commit = self[sha.encode('ascii')]
        tree = self[commit.tree]
        return self._get_file_from_tree(filename, tree)


class Scanner(object):

    def __init__(self, conf):
        self.conf = conf
        self.reporoot = self.conf.reporoot
        self._repo = RenoRepo(self.reporoot)

    def _get_ref(self, name):
        if name:
            candidates = [
                'refs/heads/' + name,
                'refs/remotes/' + name,
                'refs/tags/' + name,
                # If a stable branch was removed, look for its EOL tag.
                'refs/tags/' + (name.rpartition('/')[-1] + '-eol'),
            ]
            for ref in candidates:
                key = ref.encode('utf-8')
                if key in self._repo.refs:
                    sha = self._repo.refs[key]
                    o = self._repo[sha]
                    if isinstance(o, objects.Tag):
                        # Branches point directly to commits, but
                        # signed tags point to the signature and we
                        # need to dereference it to get to the commit.
                        sha = o.object[1]
                    return sha
            # If we end up here we didn't find any of the candidates.
            raise ValueError('Unknown reference {!r}'.format(name))
        return self._repo.refs[b'HEAD']

    def _get_walker_for_branch(self, branch):
        branch_head = self._get_ref(branch)
        return self._repo.get_walker(branch_head)

    def _get_tags_on_branch(self, branch):
        "Return a list of tag names on the given branch."
        results = []
        for c in self._get_walker_for_branch(branch):
            # shas_to_tags has encoded versions of the shas
            # but the commit object gives us a decoded version
            sha = c.commit.sha().hexdigest().encode('ascii')
            tags = self._repo.get_tags_on_commit(sha)
            results.extend(tags)
        return results

    def _get_current_version(self, branch=None):
        "Return the current version of the repository, like git describe."
        # This is similar to _get_tags_on_branch() except that it
        # counts up to where the tag appears and it returns when it
        # finds the first tagged commit (there is no need to scan the
        # rest of the branch).
        commit = self._repo[self._get_ref(branch)]
        count = 0
        while commit:
            # shas_to_tags has encoded versions of the shas
            # but the commit object gives us a decoded version
            sha = commit.sha().hexdigest().encode('ascii')
            tags = self._repo.get_tags_on_commit(sha)
            if tags:
                if count:
                    val = '{}-{}'.format(tags[-1], count)
                else:
                    val = tags[-1]
                return val
            if commit.parents:
                # Only traverse the first parent of each node.
                commit = self._repo[commit.parents[0]]
                count += 1
            else:
                commit = None
        return '0.0.0'

    def _get_branch_base(self, branch):
        "Return the tag at base of the branch."
        # Based on
        # http://stackoverflow.com/questions/1527234/finding-a-branch-point-with-git
        # git rev-list $(git rev-list --first-parent \
        #   ^origin/stable/newton master | tail -n1)^^!
        #
        # Build the set of all commits that appear on the master
        # branch, then scan the commits that appear on the specified
        # branch until we find something that is on both.
        master_commits = set(
            c.commit.sha().hexdigest()
            for c in self._get_walker_for_branch('master')
        )
        for c in self._get_walker_for_branch(branch):
            if c.commit.sha().hexdigest() in master_commits:
                # We got to this commit via the branch, but it is also
                # on master, so this is the base.
                tags = self._repo.get_tags_on_commit(
                    c.commit.sha().hexdigest().encode('ascii'))
                return tags[-1]
        return None

    def _topo_traversal(self, branch):
        """Generator that yields the branch entries in topological order.

        The topo ordering in dulwich does not match the git command line
        output, so we have our own that follows the branch being merged
        into the mainline before following the mainline. This ensures that
        tags on the mainline appear in the right place relative to the
        merge points, regardless of the commit date on the entry.

        # *   d1239b6 (HEAD -> master) Merge branch 'new-branch'
        # |\
        # | * 9478612 (new-branch) one commit on branch
        # * | 303e21d second commit on master
        # * | 0ba5186 first commit on master
        # |/
        # *   a7f573d original commit on master

        """
        head = self._get_ref(branch)

        # Map SHA values to Entry objects, because we will be traversing
        # commits not entries.
        all = {}

        children = {}

        # Populate all and children structures by traversing the
        # entire graph once. It doesn't matter what order we do this
        # the first time, since we're just recording the relationships
        # of the nodes.
        for e in self._repo.get_walker(head):
            all[e.commit.id] = e
            for p in e.commit.parents:
                children.setdefault(p, set()).add(e.commit.id)

        # Track what we have already emitted.
        emitted = set()

        # Use a deque as a stack with the nodes left to process. This
        # lets us avoid recursion, since we have no idea how deep some
        # branches might be.
        todo = collections.deque()
        todo.appendleft(head)

        while todo:
            sha = todo.popleft()
            entry = all[sha]

            # If a node has multiple children, it is the start point
            # for a branch that was merged back into the rest of the
            # tree. We will have already processed the merge commit
            # and are traversing either the branch that was merged in
            # or the base into which it was merged. We want to stop
            # traversing the branch that was merged in at the point
            # where the branch was created, because we are trying to
            # linearize the history. At that point, we go back to the
            # merge node and take the other parent node, which should
            # lead us back to the origin of the branch through the
            # mainline.
            unprocessed_children = [
                c
                for c in children.get(sha, set())
                if c not in emitted
            ]

            if not unprocessed_children:
                # All children have been processed. Remember that we have
                # processed this node and then emit the entry.
                emitted.add(sha)
                yield entry

                # Now put the parents on the stack from left to right
                # so they are processed right to left. If the node is
                # already on the stack, leave it to be processed in
                # the original order where it was added.
                #
                # NOTE(dhellmann): It's not clear if this is the right
                # solution, or if we should re-stack and then ignore
                # duplicate emissions at the top of this
                # loop. Checking if the item is already on the todo
                # stack isn't very expensive, since we don't expect it
                # to grow very large, but it's not clear the output
                # will be produced in the right order.
                for p in entry.commit.parents:
                    if p not in todo:
                        todo.appendleft(p)

            else:
                # Has unprocessed children.  Do not emit, and do not
                # restack, since when we get to the other child they will
                # stack it.
                pass

    def get_file_at_commit(self, filename, sha):
        "Return the contents of the file if it exists at the commit, or None."
        return self._repo.get_file_at_commit(filename, sha)

    def _file_exists_at_commit(self, filename, sha):
        "Return true if the file exists at the given commit."
        return bool(self.get_file_at_commit(filename, sha))

    def get_notes_by_version(self):
        """Return an OrderedDict mapping versions to lists of notes files.

        The versions are presented in reverse chronological order.

        Notes files are associated with the earliest version for which
        they were available, regardless of whether they changed later.

        :param reporoot: Path to the root of the git repository.
        :type reporoot: str
        """

        reporoot = self.reporoot
        notesdir = self.conf.notespath
        branch = self.conf.branch
        earliest_version = self.conf.earliest_version
        collapse_pre_releases = self.conf.collapse_pre_releases
        stop_at_branch_base = self.conf.stop_at_branch_base

        LOG.info('scanning %s/%s (branch=%s)',
                 reporoot.rstrip('/'), notesdir.lstrip('/'),
                 branch or '*current*')

        # Determine all of the tags known on the branch, in their date
        # order. We scan the commit history in topological order to ensure
        # we have the commits in the right version, so we might encounter
        # the tags in a different order during that phase.
        versions_by_date = self._get_tags_on_branch(branch)
        LOG.debug('versions by date %r' % (versions_by_date,))
        if earliest_version and earliest_version not in versions_by_date:
            raise ValueError(
                'earliest-version set to unknown revision {!r}'.format(
                    earliest_version))

        # If the user has told us where to stop, use that as the
        # default.
        branch_base_tag = earliest_version

        # If the user has not told us where to stop, try to work it out
        # for ourselves. If branch is set and is not "master", then we
        # want to stop at the base of the branch.
        if (stop_at_branch_base and
                (not earliest_version) and branch and (branch != 'master')):
            LOG.debug('determining earliest_version from branch')
            earliest_version = self._get_branch_base(branch)
            branch_base_tag = earliest_version
            if earliest_version and collapse_pre_releases:
                if PRE_RELEASE_RE.search(earliest_version):
                    # The earliest version won't actually be the pre-release
                    # that might have been tagged when the branch was created,
                    # but the final version. Strip the pre-release portion of
                    # the version number.
                    earliest_version = '.'.join(
                        earliest_version.split('.')[:-1]
                    )
        if earliest_version:
            LOG.info('earliest version to include is %s', earliest_version)
        else:
            LOG.info('including entire branch history')
        if branch_base_tag:
            LOG.info('stopping scan at %s', branch_base_tag)

        versions = []
        earliest_seen = collections.OrderedDict()

        # Determine the current version, which might be an unreleased or
        # dev version if there are unreleased commits at the head of the
        # branch in question. Since the version may not already be known,
        # make sure it is in the list of versions by date. And since it is
        # the most recent version, go ahead and insert it at the front of
        # the list.
        current_version = self._get_current_version(branch)
        LOG.debug('current repository version: %s' % current_version)
        if current_version not in versions_by_date:
            versions_by_date.insert(0, current_version)

        # Remember the most current filename for each id, to allow for
        # renames.
        last_name_by_id = {}

        # Remember uniqueids that have had files deleted.
        uniqueids_deleted = set()

        for counter, entry in enumerate(self._topo_traversal(branch), 1):

            sha = entry.commit.id
            tags_on_commit = self._repo.get_tags_on_commit(sha)

            LOG.debug('%06d %s %s', counter, sha, tags_on_commit)

            # If there are no tags in this block, assume the most recently
            # seen version.
            tags = tags_on_commit
            if not tags:
                tags = [current_version]
            else:
                current_version = tags_on_commit[-1]
                LOG.info('%06d %s updating current version to %s',
                         counter, sha, current_version)

            # Remember each version we have seen.
            if current_version not in versions:
                LOG.debug('%s is a new version' % current_version)
                versions.append(current_version)

            # Look for changes to notes files in this commit.
            changes = _changes_in_subdir(self._repo, entry, notesdir)
            for change in _aggregate_changes(entry, changes, notesdir):
                uniqueid = change[0]

                # Update the "earliest" version where a UID appears
                # every time we see it, because we are scanning the
                # history in reverse order so "early" items come
                # later.
                LOG.debug('%s: setting earliest reference to %s',
                          uniqueid, current_version)
                earliest_seen[uniqueid] = current_version

                c_type = change[1]

                # If we have recorded that a UID was deleted, that
                # means that was the last change made to the file and
                # we can ignore it.
                if uniqueid in uniqueids_deleted:
                    LOG.debug(
                        '%s: has already been deleted, ignoring this change',
                        uniqueid,
                    )
                    continue

                if c_type == diff_tree.CHANGE_ADD:
                    # A note is being added in this commit. If we have
                    # not seen it before, it was added here and never
                    # changed.
                    if uniqueid not in last_name_by_id:
                        path, sha = change[-2:]
                        fullpath = os.path.join(notesdir, path)
                        last_name_by_id[uniqueid] = (fullpath,
                                                     sha.decode('ascii'))
                        LOG.info(
                            '%s: update to %s in commit %s',
                            uniqueid, path, sha,
                        )
                    else:
                        LOG.debug(
                            '%s: add for file we have already seen',
                            uniqueid,
                        )

                elif c_type == diff_tree.CHANGE_DELETE:
                    # This file is being deleted without a rename. If
                    # we have already seen the UID before, that means
                    # that after the file was deleted another file
                    # with the same UID was added back. In that case
                    # we do not want to treat it as deleted.
                    #
                    # Never store deleted files in last_name_by_id so
                    # we can safely use all of those entries to build
                    # the history data.
                    if uniqueid not in last_name_by_id:
                        uniqueids_deleted.add(uniqueid)
                        LOG.info(
                            '%s: note deleted in %s',
                            uniqueid, sha,
                        )
                    else:
                        LOG.debug(
                            '%s: delete for file re-added after the delete',
                            uniqueid,
                        )

                elif c_type == diff_tree.CHANGE_RENAME:
                    # The file is being renamed. We may have seen it
                    # before, if there were subsequent modifications,
                    # so only store the name information if it is not
                    # there already.
                    if uniqueid not in last_name_by_id:
                        path, sha = change[-2:]
                        fullpath = os.path.join(notesdir, path)
                        last_name_by_id[uniqueid] = (fullpath,
                                                     sha.decode('ascii'))
                        LOG.info(
                            '%s: update to %s in commit %s',
                            uniqueid, path, sha,
                        )
                    else:
                        LOG.debug(
                            '%s: renamed file already known with the new name',
                            uniqueid,
                        )

                elif c_type == diff_tree.CHANGE_MODIFY:
                    # An existing file is being modified. We may have
                    # seen it before, if there were subsequent
                    # modifications, so only store the name
                    # information if it is not there already.
                    if uniqueid not in last_name_by_id:
                        path, sha = change[-2:]
                        fullpath = os.path.join(notesdir, path)
                        last_name_by_id[uniqueid] = (fullpath,
                                                     sha.decode('ascii'))
                        LOG.info(
                            '%s: update to %s in commit %s',
                            uniqueid, path, sha,
                        )
                    else:
                        LOG.debug(
                            '%s: modified file already known',
                            uniqueid,
                        )

                else:
                    raise ValueError(
                        'unknown change instructions {!r}'.format(change)
                    )

            if branch_base_tag and branch_base_tag in tags:
                LOG.info('reached end of branch after %d commits', counter)
                break

        # Invert earliest_seen to make a list of notes files for each
        # version.
        files_and_tags = collections.OrderedDict()
        for v in versions:
            files_and_tags[v] = []
        # Produce a list of the actual files present in the repository. If
        # a note is removed, this step should let us ignore it.
        for uniqueid, version in earliest_seen.items():
            try:
                base, sha = last_name_by_id[uniqueid]
                files_and_tags[version].append((base, sha))
            except KeyError:
                # Unable to find the file again, skip it to avoid breaking
                # the build.
                msg = ('unable to find release notes file associated '
                       'with unique id %r, skipping') % uniqueid
                LOG.debug(msg)
                print(msg, file=sys.stderr)

        # Combine pre-releases into the final release, if we are told to
        # and the final release exists.
        if collapse_pre_releases:
            collapsing = files_and_tags
            files_and_tags = collections.OrderedDict()
            for ov in versions_by_date:
                if ov not in collapsing:
                    # We don't need to collapse this one because there are
                    # no notes attached to it.
                    continue
                pre_release_match = PRE_RELEASE_RE.search(ov)
                LOG.debug('checking %r', ov)
                if pre_release_match:
                    # Remove the trailing pre-release part of the version
                    # from the string.
                    pre_rel_str = pre_release_match.groups()[0]
                    canonical_ver = ov[:-len(pre_rel_str)].rstrip('.')
                    if canonical_ver not in versions_by_date:
                        # This canonical version was never tagged, so we
                        # do not want to collapse the pre-releases. Reset
                        # to the original version.
                        canonical_ver = ov
                    else:
                        LOG.debug('combining into %r', canonical_ver)
                else:
                    canonical_ver = ov
                if canonical_ver not in files_and_tags:
                    files_and_tags[canonical_ver] = []
                files_and_tags[canonical_ver].extend(collapsing[ov])

        # Only return the parts of files_and_tags that actually have
        # filenames associated with the versions.
        trimmed = collections.OrderedDict()
        for ov in versions_by_date:
            if not files_and_tags.get(ov):
                continue
            # Sort the notes associated with the version so they are in a
            # deterministic order, to avoid having the same data result in
            # different output depending on random factors. Earlier
            # versions of the scanner assumed the notes were recorded in
            # chronological order based on the commit date, but with the
            # change to use topological sorting that is no longer
            # necessarily true. We want the notes to always show up in the
            # same order, but it doesn't really matter what order that is,
            # so just sort based on the unique id.
            trimmed[ov] = sorted(files_and_tags[ov])
            # If we have been told to stop at a version, we can do that
            # now.
            if earliest_version and ov == earliest_version:
                break

        LOG.debug(
            'found %d versions and %d files',
            len(trimmed.keys()), sum(len(ov) for ov in trimmed.values()),
        )
        return trimmed
