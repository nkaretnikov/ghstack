from __future__ import print_function

import contextlib
import io
import logging
import os
import re
import shutil
import sys
import tempfile
import unittest
from typing import Dict, Iterator, List, NewType, Optional, Tuple

import expecttest

import ghstack.github
import ghstack.github_fake
import ghstack.land
import ghstack.shell
import ghstack.submit
import ghstack.unlink
from ghstack.types import GitCommitHash


@contextlib.contextmanager
def captured_output() -> Iterator[Tuple[io.StringIO, io.StringIO]]:
    new_out, new_err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = new_out, new_err
        yield sys.stdout, sys.stderr
    finally:
        sys.stdout, sys.stderr = old_out, old_err


# TODO: Figure out how to make all git stuff in memory, so it runs
# faster.  Need to work on OSX.


GH_KEEP_TMP = os.getenv('GH_KEEP_TMP')


SubstituteRev = NewType('SubstituteRev', str)


def strip_trailing_whitespace(text: str) -> str:
    return re.sub(r' +$', '', text, flags=re.MULTILINE)


def indent(text: str, prefix: str) -> str:
    return ''.join(prefix + line if line.strip() else line
                   for line in text.splitlines(True))


class TestGh(expecttest.TestCase):
    github: ghstack.github.GitHubEndpoint
    rev_map: Dict[SubstituteRev, GitCommitHash]
    upstream_sh: ghstack.shell.Shell
    sh: ghstack.shell.Shell

    def setUp(self) -> None:
        # Set up a "parent" repository with an empty initial commit that we'll operate on
        upstream_dir = tempfile.mkdtemp()
        if GH_KEEP_TMP:
            self.addCleanup(lambda: print("upstream_dir preserved at: {}".format(upstream_dir)))
        else:
            self.addCleanup(lambda: shutil.rmtree(upstream_dir))
        self.upstream_sh = ghstack.shell.Shell(cwd=upstream_dir, testing=True)
        self.github = ghstack.github_fake.FakeGitHubEndpoint(self.upstream_sh)

        local_dir = tempfile.mkdtemp()
        if GH_KEEP_TMP:
            self.addCleanup(lambda: print("local_dir preserved at: {}".format(local_dir)))
        else:
            self.addCleanup(lambda: shutil.rmtree(local_dir))
        self.sh = ghstack.shell.Shell(cwd=local_dir, testing=True)
        self.sh.git("clone", upstream_dir, ".")

        self.rev_map = {}
        self.substituteRev(GitCommitHash("HEAD"), SubstituteRev("rINI0"))

    def writeFileAndAdd(self, filename: str, contents: str) -> None:
        with self.sh.open(filename, "w") as f:
            f.write(contents)
        self.sh.git("add", filename)

    def lookupRev(self, substitute: str) -> GitCommitHash:
        return self.rev_map[SubstituteRev(substitute)]

    def substituteRev(self, rev: str, substitute: str) -> None:
        # short doesn't really have to be here if we do substituteRev
        h = GitCommitHash(self.sh.git("rev-parse", "--short", rev))
        self.rev_map[SubstituteRev(substitute)] = h
        print("substituteRev: {} = {}".format(substitute, h))
        self.substituteExpected(h, substitute)

    # NB: returns earliest first
    def gh(self, msg: str = 'Update',
           update_fields: bool = False,
           short: bool = False,
           no_skip: bool = False) -> List[Optional[ghstack.submit.DiffMeta]]:
        return ghstack.submit.main(
            msg=msg,
            username='ezyang',
            github=self.github,
            sh=self.sh,
            update_fields=update_fields,
            stack_header='Stack',
            repo_owner='pytorch',
            repo_name='pytorch',
            short=short,
            no_skip=no_skip,
            github_url='github.com',
            remote_name='origin')

    def gh_land(self, pull_request: str) -> None:
        return ghstack.land.main(
            remote_name='origin',
            pull_request=pull_request,
            github=self.github,
            sh=self.sh,
            github_url="github.com",
        )

    def gh_unlink(self) -> None:
        ghstack.unlink.main(
            github=self.github,
            sh=self.sh,
            repo_owner='pytorch',
            repo_name='pytorch',
            github_url='github.com',
            remote_name='origin',
        )

    def dump_github(self) -> str:
        r = self.github.graphql("""
          query {
            repository(name: "pytorch", owner: "pytorch") {
              pullRequests {
                nodes {
                  number
                  baseRefName
                  headRefName
                  title
                  body
                }
              }
            }
          }
        """)
        prs = []
        refs = ""
        for pr in r['data']['repository']['pullRequests']['nodes']:
            pr['body'] = indent(pr['body'].replace('\r', ''), '    ')
            pr['commits'] = self.upstream_sh.git("log", "--reverse", "--pretty=format:%h %s", pr["baseRefName"] + ".." + pr["headRefName"])
            pr['commits'] = indent(pr['commits'], '     * ')
            prs.append("#{number} {title} ({headRefName} -> {baseRefName})\n\n"
                       "{body}\n\n{commits}\n\n".format(**pr))
            # TODO: Use of git --graph here is a bit of a loaded
            # footgun, because git doesn't really give any guarantees
            # about what the graph should look like.  So there isn't
            # really any assurance that this will output the same thing
            # on multiple test runs.  We'll have to reimplement this
            # ourselves to do it right.
            refs = self.upstream_sh.git("log", "--graph", "--oneline", "--branches=gh/*/*/head", "--decorate")
        return "".join(prs) + "Repository state:\n\n" + indent(strip_trailing_whitespace(refs), '    ') + "\n\n"

    # ------------------------------------------------------------------------- #

    def test_simple(self) -> None:
        print("####################")
        print("### test_simple")
        print("###")

        print("### First commit")
        self.writeFileAndAdd("a", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        # Just to test what happens if we use those branches
        self.sh.git("checkout", "gh/ezyang/1/orig")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 2\n\nThis is my second commit")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    This is my second commit

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_when_malform_gh_branch_exist(self) -> None:
        print("####################")
        print("### test_when_malform_gh_branch_exist")
        print("###")
        # Ensure that even if there are gh/{} branch that doesn't conform with
        # ghstack naming convension, it still works
        self.sh.git("checkout", "-b", "gh/ezyang/malform")
        self.sh.git("push", "origin", "gh/ezyang/malform")
        self.sh.git("checkout", "-b", "gh/ezyang/non_int/head")
        self.sh.git("push", "origin", "gh/ezyang/non_int/head")
        self.sh.git("checkout", "master")

        # It is doing same thing as test_simple from this point forward.
        print("### First commit")
        self.writeFileAndAdd("a", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/non_int/head, gh/ezyang/malform, gh/ezyang/1/base) Initial commit

''')
        print("###")
        print("### Second commit")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 2\n\nThis is my second commit")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    This is my second commit

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/non_int/head, gh/ezyang/malform, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_empty_commit(self) -> None:
        print("####################")
        print("### test_empty_commit")
        print("###")

        print("### Empty commit")
        self.sh.git("commit", "--allow-empty", "-m", "Commit 1\n\nThis is my first commit")
        self.writeFileAndAdd("bar", "baz")
        self.sh.git("commit", "-m", "Commit 2")

        self.sh.test_tick()
        self.gh('Initial')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 2 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500



     * rMRG1 Commit 2

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 2
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_strip_mentions(self) -> None:
        self.writeFileAndAdd("bar", "baz")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit, hello @foobar @Ivan")

        self.sh.test_tick()
        self.gh('Initial')

        self.github.patch("repos/pytorch/pytorch/pulls/500",
                          body="""\
Stack:
* **#500 Commit 1**

cc @foobar @Ivan""",
                          title="This is my first commit")

        self.sh.test_tick()
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "--amend")
        self.gh('Update 1')

        # Ensure no mentions in the log
        self.assertExpectedInline(self.sh.git("log", "--format=%B", "-n1", "origin/gh/ezyang/1/head"), '''\
Update 1 on "This is my first commit"


cc foobar Ivan

[ghstack-poisoned]''')
        self.assertExpectedInline(self.sh.git("log", "--format=%B", "-n1", "origin/gh/ezyang/1/orig"), '''\
Commit 1

This is my first commit, hello foobar Ivan

ghstack-source-id: 36c3df70a403234bbd5005985399205a8109950b
Pull Request resolved: https://github.com/pytorch/pytorch/pull/500''')

    # ------------------------------------------------------------------------- #

    def test_commit_amended_to_empty(self) -> None:
        print("####################")
        print("### test_empty_commit")
        print("###")

        self.writeFileAndAdd("bar", "baz")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")

        self.sh.test_tick()
        self.gh('Initial')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        self.sh.git('rm', 'bar')
        self.sh.git("commit", "--amend", "--allow-empty")
        self.sh.test_tick()
        self.gh('Update')
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_amend(self) -> None:
        print("####################")
        print("### test_amend")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')
        print("###")
        print("### Amend the commit")
        self.writeFileAndAdd("file1.txt", "ABBA")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.substituteRev("HEAD", "rCOM2")
        self.sh.test_tick()
        self.gh('Update A')
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG2")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1
     * rMRG2 Update A on "Commit 1"

Repository state:

    * rMRG2 (gh/ezyang/1/head) Update A on "Commit 1"
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_amend_message_only(self) -> None:
        print("####################")
        print("### test_amend")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')
        print("###")
        print("### Amend the commit")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("filter-branch", "-f", "--msg-filter", "cat && echo 'blargle'", "HEAD~..HEAD")
        self.substituteRev("HEAD", "rCOM2")
        self.sh.test_tick()
        self.gh('Update A', no_skip=True)
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG2")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1
     * rMRG2 Update A on "Commit 1"

Repository state:

    * rMRG2 (gh/ezyang/1/head) Update A on "Commit 1"
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_amend_out_of_date(self) -> None:
        print("####################")
        print("### test_amend_out_of_date")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        old_head = self.sh.git("rev-parse", "HEAD")

        print("###")
        print("### Amend the commit")
        self.writeFileAndAdd("file1.txt", "ABBA")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.sh.test_tick()
        self.gh('Update A')

        # Reset to the old version
        self.sh.git("reset", "--hard", old_head)
        self.writeFileAndAdd("file1.txt", "BAAB")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.sh.test_tick()
        self.assertRaises(RuntimeError, lambda: self.gh('Update B'))

    # ------------------------------------------------------------------------- #

    def test_multi(self) -> None:
        print("####################")
        print("### test_multi")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()

        self.gh('Initial 1 and 2')
        self.substituteRev("HEAD~", "rCOM1")
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_amend_top(self) -> None:
        print("####################")
        print("### test_amend_top")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')
        print("###")
        print("### Amend the top commit")
        self.writeFileAndAdd("file2.txt", "BAAB")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.substituteRev("HEAD", "rCOM2A")
        self.sh.test_tick()
        self.gh('Update A')
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2A")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2
     * rMRG2A Update A on "Commit 2"

Repository state:

    * rMRG2A (gh/ezyang/2/head) Update A on "Commit 2"
    * rMRG2 Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_amend_bottom(self) -> None:
        print("####################")
        print("### test_amend_bottom")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Amend the bottom commit")
        self.sh.git("checkout", "HEAD~")
        self.writeFileAndAdd("file1.txt", "ABBA")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.substituteRev("HEAD", "rCOM1A")
        self.sh.test_tick()
        self.gh('Update A')
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1A")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1
     * rMRG1A Update A on "Commit 1"

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG1A (gh/ezyang/1/head) Update A on "Commit 1"
    | * rMRG2 (gh/ezyang/2/head) Commit 2
    |/
    * rMRG1 (gh/ezyang/2/base) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Restack the top commit")
        self.sh.git("cherry-pick", self.lookupRev("rCOM2"))
        self.sh.test_tick()
        self.gh('Update B')
        self.substituteRev("HEAD", "rCOM2A")
        self.substituteRev("origin/gh/ezyang/2/base", "rINI2A")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2A")
        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1
     * rMRG1A Update A on "Commit 1"

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2
     * rMRG2A Update B on "Commit 2"

Repository state:

    *   rMRG2A (gh/ezyang/2/head) Update B on "Commit 2"
    |\\
    | * rINI2A (gh/ezyang/2/base) Update base for Update B on "Commit 2"
    * | rMRG2 Commit 2
    |/
    | * rMRG1A (gh/ezyang/1/head) Update A on "Commit 1"
    |/
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_amend_all(self) -> None:
        print("####################")
        print("### test_amend_all")
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Amend the commits")
        self.sh.git("checkout", "HEAD~")
        self.writeFileAndAdd("file1.txt", "ABBA")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.substituteRev("HEAD", "rCOM1A")
        self.sh.test_tick()

        self.sh.git("cherry-pick", self.lookupRev("rCOM2"))
        self.substituteRev("HEAD", "rCOM2A")
        self.sh.test_tick()

        self.gh('Update A')
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1A")
        self.substituteRev("origin/gh/ezyang/2/base", "rINI2A")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2A")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1
     * rMRG1A Update A on "Commit 1"

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2
     * rMRG2A Update A on "Commit 2"

Repository state:

    * rMRG1A (gh/ezyang/1/head) Update A on "Commit 1"
    | *   rMRG2A (gh/ezyang/2/head) Update A on "Commit 2"
    | |\\
    | | * rINI2A (gh/ezyang/2/base) Update base for Update A on "Commit 2"
    | |/
    |/|
    | * rMRG2 Commit 2
    |/
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_rebase(self) -> None:
        print("####################")
        print("### test_rebase")

        self.sh.git("checkout", "-b", "feature")

        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Push master forward")
        self.sh.git("checkout", "master")
        self.writeFileAndAdd("master.txt", "M")
        self.sh.git("commit", "-m", "Master commit 1\n\nA commit with a M")
        self.substituteRev("HEAD", "rINI2")
        self.sh.test_tick()
        self.sh.git("push", "origin", "master")

        print("###")
        print("### Rebase the commits")
        self.sh.git("checkout", "feature")
        self.sh.git("rebase", "origin/master")

        self.substituteRev("HEAD", "rCOM2A")
        self.substituteRev("HEAD~", "rCOM1A")

        self.gh('Rebase')
        self.substituteRev("origin/gh/ezyang/1/base", "rINI1A")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1A")
        self.substituteRev("origin/gh/ezyang/2/base", "rINI2A")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2A")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1
     * rMRG1A Rebase on "Commit 1"

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2
     * rMRG2A Rebase on "Commit 2"

Repository state:

    *   rMRG1A (gh/ezyang/1/head) Rebase on "Commit 1"
    |\\
    | *   rINI1A (gh/ezyang/1/base) Update base for Rebase on "Commit 1"
    | |\\
    | | | *   rMRG2A (gh/ezyang/2/head) Rebase on "Commit 2"
    | | | |\\
    | | | | * rINI2A (gh/ezyang/2/base) Update base for Rebase on "Commit 2"
    | |_|_|/|
    |/| | |/
    | | |/|
    | | * | rINI2 (HEAD -> master) Master commit 1
    | |/ /
    | | * rMRG2 Commit 2
    | |/
    |/|
    * | rMRG1 Commit 1
    |/
    * rINI0 Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_cherry_pick(self) -> None:
        print("####################")
        print("### test_cherry_pick")

        self.sh.git("checkout", "-b", "feature")

        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Push master forward")
        self.sh.git("checkout", "master")
        self.writeFileAndAdd("master.txt", "M")
        self.sh.git("commit", "-m", "Master commit 1\n\nA commit with a M")
        self.substituteRev("HEAD", "rINI2")
        self.sh.test_tick()
        self.sh.git("push", "origin", "master")

        print("###")
        print("### Cherry-pick the second commit")
        self.sh.git("cherry-pick", "feature")

        self.substituteRev("HEAD", "rCOM2A")

        self.gh('Cherry pick')
        self.substituteRev("origin/gh/ezyang/2/base", "rINI2A")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2A")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501

    A commit with a B

     * rMRG2 Commit 2
     * rMRG2A Cherry pick on "Commit 2"

Repository state:

    *   rMRG2A (gh/ezyang/2/head) Cherry pick on "Commit 2"
    |\\
    | *   rINI2A (gh/ezyang/2/base) Update base for Cherry pick on "Commit 2"
    | |\\
    | | * rINI2 (HEAD -> master) Master commit 1
    * | | rMRG2 Commit 2
    |/ /
    * / rMRG1 (gh/ezyang/1/head) Commit 1
    |/
    * rINI0 (gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_reorder(self) -> None:
        self.writeFileAndAdd('file1.txt', 'A')
        self.sh.git('commit', '-m', 'Commit 1\n\nA commit with an A')
        self.sh.test_tick()

        self.writeFileAndAdd('file2.txt', 'B')
        self.sh.git('commit', '-m', 'Commit 2\n\nA commit with an B')
        self.sh.test_tick()

        self.gh('Initial')
        self.sh.test_tick()
        self.substituteRev('origin/gh/ezyang/1/head', 'rMRG1')
        self.substituteRev('origin/gh/ezyang/2/head', 'rMRG2')

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with an B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        # https://stackoverflow.com/a/16205257
        self.sh.git('rebase', '--onto', 'HEAD~2', 'HEAD~', 'HEAD')
        self.sh.test_tick()
        self.sh.git('cherry-pick', 'master~')
        self.sh.test_tick()

        self.gh('Reorder')
        self.sh.test_tick()
        self.substituteRev('origin/gh/ezyang/1/base', 'rINI1A')
        self.substituteRev('origin/gh/ezyang/1/head', 'rMRG1A')
        self.substituteRev('origin/gh/ezyang/2/base', 'rINI2A')
        self.substituteRev('origin/gh/ezyang/2/head', 'rMRG2A')

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500
    * #501

    A commit with an A

     * rMRG1 Commit 1
     * rMRG1A Reorder on "Commit 1"

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * #500
    * __->__ #501

    A commit with an B

     * rMRG2 Commit 2
     * rMRG2A Reorder on "Commit 2"

Repository state:

    *   rMRG1A (gh/ezyang/1/head) Reorder on "Commit 1"
    |\\
    | * rINI1A (gh/ezyang/1/base) Update base for Reorder on "Commit 1"
    | | *   rMRG2A (gh/ezyang/2/head) Reorder on "Commit 2"
    | | |\\
    | | | * rINI2A (gh/ezyang/2/base) Update base for Reorder on "Commit 2"
    | |_|/
    |/| |
    | | * rMRG2 Commit 2
    | |/
    |/|
    * | rMRG1 Commit 1
    |/
    * rINI0 (HEAD -> master) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_no_clobber(self) -> None:
        # Check that we don't clobber changes to PR description or title

        print("####################")
        print("### test_no_clobber")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nOriginal message")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Amend the PR")
        self.github.patch("repos/pytorch/pytorch/pulls/500",
                          body="""\
Stack:
* **#500 Commit 1**

Directly updated message body""",
                          title="Directly updated title")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Directly updated title (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * **#500 Commit 1**

    Directly updated message body

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Submit an update")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "--amend")
        self.sh.test_tick()
        self.gh('Update 1')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Directly updated title (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Directly updated message body

     * rMRG1 Commit 1
     * rMRG2 Update 1 on "Directly updated title"

Repository state:

    * rMRG2 (gh/ezyang/1/head) Update 1 on "Directly updated title"
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_no_clobber_carriage_returns(self) -> None:
        # In some situations, GitHub will replace your newlines with
        # \r\n.  Check we handle this correctly.

        print("####################")
        print("### test_no_clobber_carriage_returns")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nOriginal message")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Amend the PR")
        self.github.patch("repos/pytorch/pytorch/pulls/500",
                          body="""\
Stack:
* **#500 Commit 1**

Directly updated message body""".replace('\n', '\r\n'),
                          title="Directly updated title")

        print("###")
        print("### Submit a new commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 2")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Directly updated title (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    Directly updated message body

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500



     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_reject_head_stack(self) -> None:
        self.writeFileAndAdd("a", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        self.gh('Initial 1')

        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.sh.git("checkout", "gh/ezyang/1/head")

        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 2\n\nThis is my second commit")
        self.sh.test_tick()

        self.assertRaises(RuntimeError, lambda: self.gh('Initial 2'))

    # ------------------------------------------------------------------------- #

    def test_update_fields(self) -> None:
        # Check that we do clobber fields when explicitly asked

        print("####################")
        print("### test_update_fields")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nOriginal message")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Amend the PR")
        self.github.patch("repos/pytorch/pytorch/pulls/500",
                          body="Directly updated message body",
                          title="Directly updated title")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Directly updated title (gh/ezyang/1/head -> gh/ezyang/1/base)

    Directly updated message body

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Force update fields")
        self.gh('Update 1', update_fields=True)
        self.sh.test_tick()

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

     * rMRG1 Commit 1
     * 49615a9 Update 1 on "Commit 1"

Repository state:

    * 49615a9 (gh/ezyang/1/head) Update 1 on "Commit 1"
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_update_fields_preserves_commit_message(self) -> None:
        # Check that we do clobber fields when explicitly asked

        print("####################")
        print("### test_update_fields")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nOriginal message")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Amend the commit")
        self.sh.git('filter-branch', '--msg-filter', 'echo Amended && cat', 'HEAD~..HEAD')
        self.gh('Update 1', update_fields=True)
        self.sh.test_tick()

        self.assertExpectedInline(self.dump_github(), '''\
#500 Amended (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Commit 1

    Original message

     * rMRG1 Commit 1
     * 93de014 Update 1 on "Amended"

Repository state:

    * 93de014 (gh/ezyang/1/head) Update 1 on "Amended"
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        self.assertRegex(self.sh.git('log', '--format=%B', '-n', '1', 'HEAD'), 'Amended')

    # ------------------------------------------------------------------------- #

    def test_update_fields_preserve_differential_revision(self) -> None:
        # Check that Differential Revision is preserved

        logging.info("### test_update_fields_preserve_differential_revision")
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nOriginal message")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.sh.test_tick()
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        logging.info("### Amend the PR")
        body = """\n
Directly updated message body

Differential Revision: [D14778507](https://our.internmc.facebook.com/intern/diff/D14778507)
"""
        self.github.patch("repos/pytorch/pytorch/pulls/500",
                          body=body,
                          title="Directly updated title")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Directly updated title (gh/ezyang/1/head -> gh/ezyang/1/base)



    Directly updated message body

    Differential Revision: [D14778507](https://our.internmc.facebook.com/intern/diff/D14778507)


     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        logging.info("### Force update fields")
        self.gh('Update 1', update_fields=True)
        self.sh.test_tick()

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    Original message

    Differential Revision: [D14778507](https://our.internmc.facebook.com/intern/diff/D14778507)

     * rMRG1 Commit 1
     * 0800457 Update 1 on "Commit 1"

Repository state:

    * 0800457 (gh/ezyang/1/head) Update 1 on "Commit 1"
    * rMRG1 Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_remove_bottom_commit(self) -> None:
        # This is to test a bug where we decided not to update base,
        # but this was wrong

        self.sh.git("checkout", "-b", "feature")

        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        print("###")
        print("### Second commit")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nA commit with a B")
        self.sh.test_tick()
        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with a B

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rMRG1 (gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        print("###")
        print("### Delete first commit")
        self.sh.git("checkout", "master")

        print("###")
        print("### Cherry-pick the second commit")
        self.sh.git("cherry-pick", "feature")

        self.substituteRev("HEAD", "rCOM2A")

        self.gh('Cherry pick')
        self.substituteRev("origin/gh/ezyang/2/base", "rINI2A")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2A")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501

    A commit with a B

     * rMRG2 Commit 2
     * rMRG2A Cherry pick on "Commit 2"

Repository state:

    *   rMRG2A (gh/ezyang/2/head) Cherry pick on "Commit 2"
    |\\
    | * rINI2A (gh/ezyang/2/base) Update base for Cherry pick on "Commit 2"
    * | rMRG2 Commit 2
    |/
    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_short(self) -> None:
        self.writeFileAndAdd("b", "asdf")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        with captured_output() as (out, err):
            self.gh('Initial', short=True)
        self.assertEqual(out.getvalue(), "https://github.com/pytorch/pytorch/pull/500\n")

    # ------------------------------------------------------------------------- #

    def test_land_ff(self) -> None:
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        diff, = self.gh('Initial')
        assert diff is not None
        pr_url = diff.pr_url
        # Because this is fast forward, commit will be landed exactly as is
        self.substituteRev("HEAD", "rCOM1")

        self.gh_land(pr_url)
        self.assertExpectedInline(self.upstream_sh.git("log", "--oneline", "master"), '''\
rCOM1 Commit 1
rINI0 Initial commit''')

    # ------------------------------------------------------------------------- #
    #
    def test_land_ff_stack(self) -> None:
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nThis is my second commit")
        self.sh.test_tick()
        diff1, diff2, = self.gh('Initial')
        assert diff1 is not None
        assert diff2 is not None
        pr_url = diff2.pr_url
        # Because this is fast forward, commit will be landed exactly as is
        self.substituteRev("HEAD~", "rCOM1")
        self.substituteRev("HEAD", "rCOM2")

        self.gh_land(pr_url)
        self.assertExpectedInline(self.upstream_sh.git("log", "--oneline", "master"), '''\
rCOM2 Commit 2
rCOM1 Commit 1
rINI0 Initial commit''')

    # ------------------------------------------------------------------------- #
    #
    def test_land_ff_stack_two_phase(self) -> None:
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nThis is my second commit")
        self.sh.test_tick()
        diff1, diff2, = self.gh('Initial')
        assert diff1 is not None
        assert diff2 is not None
        pr_url1 = diff1.pr_url
        pr_url2 = diff2.pr_url

        self.substituteRev("HEAD~", "rCOM1")
        self.substituteRev("HEAD", "rCOM2")

        self.gh_land(pr_url1)
        self.gh_land(pr_url2)
        self.assertExpectedInline(self.upstream_sh.git("log", "--oneline", "master"), '''\
rCOM2 Commit 2
rCOM1 Commit 1
rINI0 Initial commit''')

    # ------------------------------------------------------------------------- #
    #
    def test_land_with_early_mod(self) -> None:
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 2\n\nThis is my second commit")
        self.sh.test_tick()
        diff1, diff2, = self.gh('Initial')
        assert diff1 is not None
        assert diff2 is not None
        pr_url = diff2.pr_url

        # edit earlier commit
        self.sh.git("checkout", "HEAD~")
        self.writeFileAndAdd("file1.txt", "ABBA")
        # Can't use -m here, it will clobber the metadata
        self.sh.git("commit", "--amend")
        self.substituteRev("HEAD", "rCOM1A")
        self.gh('Update')

        self.gh_land(pr_url)
        self.assertExpectedInline(self.upstream_sh.git("show", "master:file1.txt"), '''ABBA''')
        self.assertExpectedInline(self.upstream_sh.git("show", "master:file2.txt"), '''B''')

    # ------------------------------------------------------------------------- #

    def test_land_non_ff(self) -> None:
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nThis is my first commit")
        self.sh.test_tick()
        diff, = self.gh('Initial')
        assert diff is not None
        pr_url = diff.pr_url
        self.substituteRev("HEAD", "rCOM1")

        self.sh.git("reset", "--hard", "origin/master")
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Upstream commit")
        self.substituteRev("HEAD", "rUP1")
        self.sh.git("push")

        self.sh.git("checkout", "gh/ezyang/1/orig")
        self.gh_land(pr_url)

        self.substituteRev("origin/master", "rUP2")

        self.assertExpectedInline(self.upstream_sh.git("log", "--oneline", "master"), '''\
rUP2 Commit 1
rUP1 Upstream commit
rINI0 Initial commit''')

    # ------------------------------------------------------------------------- #

    def test_unlink(self) -> None:
        print("###")
        print("### First commit")
        self.writeFileAndAdd("file1.txt", "A")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an A")
        self.sh.test_tick()
        self.writeFileAndAdd("file2.txt", "B")
        self.sh.git("commit", "-m", "Commit 1\n\nA commit with an B")
        self.sh.test_tick()
        self.gh('Initial 1')
        self.substituteRev("HEAD", "rCOM1")
        self.substituteRev("origin/gh/ezyang/1/head", "rMRG1")

        # Unlink
        self.gh_unlink()

        self.gh('Initial 2')
        self.substituteRev("HEAD", "rCOM2")
        self.substituteRev("origin/gh/ezyang/2/head", "rMRG2")

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * #501
    * __->__ #500

    A commit with an A

     * rMRG1 Commit 1

#501 Commit 1 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501
    * #500

    A commit with an B

     * rMRG2 Commit 1

#502 Commit 1 (gh/ezyang/3/head -> gh/ezyang/3/base)

    Stack:
    * #503
    * __->__ #502

    A commit with an A

     * rMRG1 Commit 1

#503 Commit 1 (gh/ezyang/4/head -> gh/ezyang/4/base)

    Stack:
    * __->__ #503
    * #502

    A commit with an B

     * rMRG2 Commit 1

Repository state:

    * rMRG2 (gh/ezyang/4/head, gh/ezyang/2/head) Commit 1
    * rMRG1 (gh/ezyang/4/base, gh/ezyang/3/head, gh/ezyang/2/base, gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/3/base, gh/ezyang/1/base) Initial commit

''')

    # ------------------------------------------------------------------------- #

    def test_default_branch_change(self) -> None:
        # make commit
        self.writeFileAndAdd('file1.txt', 'A')
        self.sh.git('commit', '-m', 'Commit 1\n\nThis is my first commit')
        self.sh.test_tick()
        # ghstack
        diff1, = self.gh('Initial 1')
        assert diff1 is not None
        self.substituteRev('origin/gh/ezyang/1/head', 'rMRG1')

        # make main branch
        self.sh.git('branch', 'main', 'master')
        self.sh.git('push', 'origin', 'main')
        # change default branch to main
        self.github.patch(
            'repos/pytorch/pytorch',
            name='pytorch',
            default_branch='main',
        )

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

Repository state:

    * rMRG1 (gh/ezyang/1/head) Commit 1
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        # land
        self.gh_land(diff1.pr_url)
        self.substituteRev('origin/main', 'rUP1')

        self.assertExpectedInline(self.upstream_sh.git('log', '--oneline', 'master'), '''\
rINI0 Initial commit''')
        self.assertExpectedInline(self.upstream_sh.git('log', '--oneline', 'main'), '''\
rUP1 Commit 1
rINI0 Initial commit''')

        # make another commit
        self.writeFileAndAdd('file2.txt', 'B')
        self.sh.git('commit', '-m', 'Commit 2\n\nThis is my second commit')
        self.sh.test_tick()
        # ghstack
        diff2, = self.gh('Initial 2')
        assert diff2 is not None
        self.substituteRev('origin/gh/ezyang/2/head', 'rMRG2')

        # change default branch back to master
        self.github.patch(
            'repos/pytorch/pytorch',
            name='pytorch',
            default_branch='master',
        )

        self.assertExpectedInline(self.dump_github(), '''\
#500 Commit 1 (gh/ezyang/1/head -> gh/ezyang/1/base)

    Stack:
    * __->__ #500

    This is my first commit

     * rMRG1 Commit 1

#501 Commit 2 (gh/ezyang/2/head -> gh/ezyang/2/base)

    Stack:
    * __->__ #501

    This is my second commit

     * rMRG2 Commit 2

Repository state:

    * rMRG2 (gh/ezyang/2/head) Commit 2
    * rUP1 (main, gh/ezyang/2/base, gh/ezyang/1/orig) Commit 1
    | * rMRG1 (gh/ezyang/1/head) Commit 1
    |/
    * rINI0 (HEAD -> master, gh/ezyang/1/base) Initial commit

''')

        # land again
        self.gh_land(diff2.pr_url)
        self.substituteRev('origin/master', 'rUP3')
        self.substituteRev('origin/master~', 'rUP2')

        self.assertExpectedInline(self.upstream_sh.git('log', '--oneline', 'master'), '''\
rUP3 Commit 2
rUP2 Commit 1
rINI0 Initial commit''')
        self.assertExpectedInline(self.upstream_sh.git('log', '--oneline', 'main'), '''\
rUP1 Commit 1
rINI0 Initial commit''')


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(message)s')
    unittest.main()
