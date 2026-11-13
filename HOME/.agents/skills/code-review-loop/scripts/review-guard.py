#!/usr/bin/env python3
"""Back up review inputs, run a reviewer, and detect persistent mutations.

Outputs are private recovery artifacts, never automatically restored. Exit 40
means mutation; 2 means guard failure; otherwise return the command's exit code.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tarfile

from review_artifacts import release_artifacts

# Persistent index flags worth detecting: assume-valid, skip-worktree and
# intent-to-add. Transient flags such as CE_UPDATE_IN_BASE (0x04000000) are
# set by ordinary stat refreshes and are masked out.
INDEX_PERSISTENT_FLAG_MASK = 0xE0000000


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def record(path):
    s = path.lstat()
    mode = stat.S_IMODE(s.st_mode)
    if stat.S_ISLNK(s.st_mode):
        return ['link', mode, os.readlink(path)]
    if stat.S_ISREG(s.st_mode):
        return ['file', mode, s.st_size, digest(path)]
    if stat.S_ISDIR(s.st_mode):
        return ['dir', mode]
    raise ValueError(f'unsupported special file: {path}')


def discover_nested(root):
    """Find nested repositories before pruning allowed output directories."""
    found = []
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != '.git']
        directory = Path(dirpath)
        if directory == root:
            continue
        marker = directory / '.git'
        if marker.exists() or marker.is_symlink():
            found.append(directory.relative_to(root).as_posix())
    return found


def tree(root, allowed, tracked, nested_roots=None):
    """Record the working tree, and list nested repositories found in it.

    A directory holding its own `.git` (a submodule, or any repository nested
    inside this one) is still walked like any other directory, but its `.git`
    directory is left to `metadata()`, which records the same Git state for
    it as for the top-level repository and, like there, skips object storage.
    A `.git` file (a gitdir pointer, as in submodules) is recorded as a file.
    Tracked files inside a nested repository are protected too, so an allowed
    output directory cannot hide a submodule-tracked file.
    """
    result, nested = {}, []
    nested_roots = list(nested_roots or [])
    protected_paths = set(tracked)
    for rel_repo in nested_roots:
        try:
            for item in git(root / rel_repo, 'ls-files', '-z').split(b'\0'):
                if item:
                    protected_paths.add(f'{rel_repo}/{os.fsdecode(item)}')
        except subprocess.CalledProcessError:
            pass

    def visit(directory):
        marker = directory / '.git'
        if directory != root and (marker.exists() or marker.is_symlink()):
            nested.append(directory.relative_to(root).as_posix())
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if rel == '.git':
                continue
            if path.name == '.git' and path.is_dir() and not path.is_symlink():
                continue
            excluded = any(rel == p or rel.startswith(p + '/') for p in allowed)
            protected = rel in protected_paths or any(
                t.startswith(rel + '/') for t in protected_paths
            )
            contains_repo = any(n == rel or n.startswith(rel + '/') for n in nested_roots)
            if excluded and not protected and not contains_repo:
                continue
            result[rel] = record(path)
            if result[rel][0] == 'dir':
                visit(path)
    visit(root)
    return result, nested


def metadata(root, marker=True):
    # Git paths can live outside the working directory in a linked worktree,
    # and a submodule's live in the superproject's .git/modules.
    names = ['HEAD', 'index', 'config', 'config.worktree', 'commondir', 'packed-refs',
             'MERGE_HEAD', 'MERGE_MSG', 'MERGE_MODE', 'ORIG_HEAD',
             'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'AUTO_MERGE',
             'rebase-merge', 'rebase-apply', 'sequencer', 'info/sparse-checkout']
    index = Path(os.fsdecode(git(root, 'rev-parse', '--git-path', 'index')).strip())
    if not index.is_absolute():
        index = root / index
    shared_names = {path.name for path in sorted(index.parent.glob('sharedindex.*'))}
    names.extend(sorted(shared_names))
    result, paths = {}, {}
    # Refs belong to the shared repository, so another linked worktree may
    # change them without changing this checkout. Protect only HEAD's symbolic
    # chain and resolved object, independent of loose/packed storage.
    current, chain = 'HEAD', []
    while True:
        if current in [entry[0] for entry in chain] or len(chain) >= 32:
            raise ValueError('cyclic or excessively deep HEAD symbolic ref')
        try:
            target = os.fsdecode(git(root, 'symbolic-ref', '--quiet', '--no-recurse', current)).strip()
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            break
        chain.append([current, target])
        if target not in names:
            names.append(target)
        current = target
    try:
        resolved = os.fsdecode(git(root, 'rev-parse', '--verify', '--quiet', 'HEAD')).strip()
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        # An unborn branch has no object yet, but malformed refs must not
        # silently look like an unborn branch.
        if not chain:
            raise ValueError('cannot resolve detached HEAD') from error
        try:
            git(root, 'show-ref', '--verify', '--quiet', current)
        except subprocess.CalledProcessError as ref_error:
            if ref_error.returncode != 1:
                raise
        resolved = None
    result['head-refs'] = chain
    result['head-object'] = resolved
    ref_names = {entry[1] for entry in chain}
    for name in names:
        path = Path(os.fsdecode(git(root, 'rev-parse', '--git-path', name)).strip())
        if not path.is_absolute():
            path = root / path
        paths[name] = path
        if name in shared_names or name in ref_names or name == 'packed-refs':
            # Preserve recovery inputs without comparing storage bytes: ref
            # packing and shared-index refreshes do not change review inputs.
            # Semantic HEAD and index state cover the relevant changes.
            continue
        if path.exists() or path.is_symlink():
            if name == 'index':
                # Git refreshes the index stat cache during read-only commands
                # such as `git status`/`git diff`. Compare the staged entries
                # (mode, blob, stage, path, assume-unchanged) plus the index
                # flags (including intent-to-add) instead of raw bytes, so
                # normal inspection is not a mutation while staging, unstaging
                # or intent-to-add changes still are.
                stage = os.fsdecode(git(root, 'ls-files', '--stage', '-v', '-z'))
                debug = git(root, 'ls-files', '--debug', '-z')
                flags = [
                    f'{int(item.decode("ascii"), 16) & INDEX_PERSISTENT_FLAG_MASK:08x}'
                    for item in re.findall(rb'flags: ([0-9a-f]+)', debug)
                ]
                result[name] = ['index-semantic', stage, flags]
            elif name == 'config':
                # Branch creation/renaming in another linked worktree updates
                # the shared config too. Only this checkout's branch config
                # affects its review inputs; retain all non-branch settings.
                current_branches = {
                    target[len('refs/heads/'):]
                    for _source, target in chain if target.startswith('refs/heads/')
                }
                entries = subprocess.check_output(
                    ['git', 'config', '--file', str(path), '--null', '--list'],
                    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
                ).split(b'\0')
                relevant = []
                for entry in entries:
                    if not entry:
                        continue
                    key = os.fsdecode(entry.partition(b'\n')[0])
                    if key.startswith('branch.'):
                        branch = key[len('branch.'):].rsplit('.', 1)[0]
                        if branch not in current_branches:
                            continue
                    relevant.append(entry.hex())
                result[name] = ['config-semantic', stat.S_IMODE(path.stat().st_mode), relevant]
            else:
                result[name] = record(path)
            if path.is_dir() and not path.is_symlink():
                for child in sorted(path.rglob('*')):
                    result[name + '/' + child.relative_to(path).as_posix()] = record(child)
    marker_path = root / '.git'
    if marker and (marker_path.is_file() or marker_path.is_symlink()):
        result['git-marker'] = record(marker_path)
        paths['git-marker'] = marker_path
    return result, paths


def snapshot(root, allowed, tracked):
    """Return the comparable state and the Git metadata paths to back up.

    The state has a `tree` area, a `git` area for the repository itself, and
    one `git[<path>]` area per nested repository (submodule or otherwise).
    Gitlinks without a repository behind them (uninitialized submodules, or
    stray gitlinks with no .gitmodules entry) need nothing extra: the index
    already records their commits, and the tree records their directories.
    """
    files, nested = tree(root, allowed, tracked, discover_nested(root))
    state, paths = {'tree': files}, {}
    state['git'], paths[''] = metadata(root)
    for rel in nested:
        repo = root / rel
        try:
            top = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve()
        except subprocess.CalledProcessError:
            top = None
        if top != repo.resolve():
            raise ValueError(f'cannot resolve the Git directory of nested repository {rel}')
        state[f'git[{rel}]'], paths[rel] = metadata(repo, marker=True)
    return state, paths


def compare(before, after):
    changes = []
    for area in sorted(before.keys() | after.keys()):
        old, new = before.get(area, {}), after.get(area, {})
        for key in sorted(old.keys() | new.keys()):
            if old.get(key) != new.get(key):
                changes.append(area + ':' + key)
    return changes


def native_result(handoff, receipt):
    """Validate coordinator-supplied host evidence, never invent CLI telemetry."""
    error = None
    if receipt.get('handoff_id') != handoff['handoff_id']:
        error = 'completion receipt does not match this attempt'
    elif receipt.get('status') != 'completed' or receipt.get('completion_verified') is not True:
        error = 'host reviewer completion is unverified'
    elif not receipt.get('host_task_id') or not receipt.get('completion_event'):
        error = 'host task and terminal completion event are required'
    elif not isinstance(receipt.get('denials_observed'), list):
        error = 'host permission denial inspection is unknown'
    observed = receipt.get('observed_model')
    verified = receipt.get('model_verified') is True
    if verified and not observed:
        error = 'verified model requires observed host model evidence'
    if observed and observed != handoff['requested_model']:
        error = 'host model differs from the exact requested model'
    result_file = receipt.get('result_file')
    if not result_file or not Path(result_file).is_absolute() or not Path(result_file).is_file():
        error = error or 'native result file is missing or is not absolute'
    elif not Path(result_file).read_text(encoding='utf-8').strip():
        error = error or 'native result is empty'
    return {'status': 'failed' if error else 'ok', 'error': error,
            'error_class': 'failed' if error else None,
            'native_host_execution': True, 'cleanup_verified': None,
            'completion_verified': receipt.get('completion_verified') is True and not error,
            'host_task_id': receipt.get('host_task_id'),
            'completion_event': receipt.get('completion_event'),
            'official_agent': handoff['official_agent'],
            'official_model_id': handoff['requested_model'],
            'observed_model': observed, 'model_verified': verified and bool(observed),
            'denials_observed': receipt.get('denials_observed'), 'result_file': result_file}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--stdin', help='prompt file (required before execution)')
    native = parser.add_mutually_exclusive_group()
    native.add_argument('--native-begin', action='store_true', help='snapshot before host delegation')
    native.add_argument('--native-finish', metavar='RECEIPT', help='verify after host completion')
    parser.add_argument('--agent', help='official host agent name for native begin')
    parser.add_argument('--model', help='exact requested native model for native begin')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if Path(os.fsdecode(git(root, 'rev-parse', '--show-toplevel')).strip()).resolve() != root:
        raise ValueError('--root must be the repository root')
    prefix = Path(args.prefix).resolve()
    if prefix.is_relative_to(root):
        raise ValueError('artifacts must live outside the repository')
    prefix.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command and not (args.native_begin or args.native_finish):
        raise ValueError('missing reviewer command')
    if command and (args.native_begin or args.native_finish):
        raise ValueError('native handoff does not take a reviewer command')
    if not args.native_finish and not args.stdin:
        raise ValueError('--stdin is required before execution')
    if args.native_begin and (not args.agent or not args.model):
        raise ValueError('native begin requires --agent and --model')
    allowed = json.loads(os.environ.get('REVIEW_OUTPUT_PATHS', '[]'))
    if not isinstance(allowed, list) or any(not isinstance(p, str) or not p or
            Path(p).is_absolute() or any(c in ('', '.', '..', '.git') for c in p.split('/')) for p in allowed):
        raise ValueError('REVIEW_OUTPUT_PATHS must be a JSON array of relative directory paths without .git or traversal')
    def save(suffix, data):
        path = Path(str(prefix) + suffix)
        with path.open('x', encoding='utf-8') as f:
            os.chmod(path, 0o600)
            json.dump(data, f, indent=2, ensure_ascii=True)
    if args.native_finish:
        handoff = json.loads(Path(str(prefix) + '.handoff.json').read_text())
        if handoff['root'] != str(root):
            raise ValueError('native handoff belongs to a different repository')
        # The begin snapshot owns exclusions and tracked paths, not a later env.
        allowed, tracked = handoff['allowed_outputs'], set(handoff['tracked'])
        before = json.loads(Path(str(prefix) + '.before.json').read_text())
        after = snapshot(root, allowed, tracked)[0]
        save('.after.json', after)
        changes = compare(before, after)
        save('.guard.json', {'command_exit': None, 'allowed_outputs': allowed,
                             'changes': changes, 'status': 'mutated' if changes else 'unchanged'})
        receipt = json.loads(Path(args.native_finish).read_text())
        result = native_result(handoff, receipt)
        save('.stdout', result)
        save('.receipt.json', receipt)
        code = 40 if changes else (0 if result['status'] == 'ok' else 1)
        Path(str(prefix) + '.exit').write_text(str(code) + '\n')
        release_artifacts(prefix)
        print(json.dumps(result))
        return code
    tracked = set(os.fsdecode(p) for p in git(root, 'ls-files', '-z').split(b'\0') if p)
    before, git_paths = snapshot(root, allowed, tracked)
    save('.before.json', before)
    # A private archive contains all protected files, including ignored files.
    archive_path = Path(str(prefix) + '.backup.tar')
    with archive_path.open('xb') as output:
        os.chmod(archive_path, 0o600)
        with tarfile.open(fileobj=output, mode='w', dereference=False) as archive:
            for rel in before['tree']:
                archive.add(root / rel, arcname='tree/' + rel, recursive=False)
            for repo, named in git_paths.items():
                base = 'nested/' + repo + '/' if repo else 'git/'
                for name, path in named.items():
                    if path.exists() or path.is_symlink():
                        archive.add(path, arcname=base + name, recursive=True)
            marker = root / '.git'
            if marker.is_file():
                archive.add(marker, arcname='git-marker', recursive=False)
    # Detect a concurrent edit during backup instead of launching on mixed inputs.
    if before != snapshot(root, allowed, tracked)[0]:
        raise ValueError('repository changed during backup; preserve artifacts and retry after edits stop')
    if args.native_begin:
        prompt = Path(args.stdin).resolve()
        handoff = {'root': str(root), 'allowed_outputs': allowed, 'tracked': sorted(tracked),
                   'handoff_id': secrets.token_hex(16), 'prompt_file': str(prompt),
                   'prompt_sha256': digest(prompt), 'official_agent': args.agent,
                   'requested_model': args.model}
        save('.handoff.json', handoff)
        print(json.dumps({'status': 'ready', **handoff}))
        return 0
    for suffix in ('.stdout', '.stderr', '.exit'):
        if Path(str(prefix) + suffix).exists():
            raise ValueError('prefix already used; choose a fresh attempt prefix')
    with open(args.stdin, 'rb') as prompt, open(str(prefix) + '.stdout', 'xb') as out, open(str(prefix) + '.stderr', 'xb') as err:
        os.chmod(out.name, 0o600)
        os.chmod(err.name, 0o600)
        code = subprocess.run(command, cwd=root, stdin=prompt, stdout=out, stderr=err,
                              env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).returncode
    Path(str(prefix) + '.exit').write_text(str(code) + '\n')
    # Keep the original tracked set: removing a path from the index cannot make
    # it become an allowed build output during the after-snapshot.
    after = snapshot(root, allowed, tracked)[0]
    save('.after.json', after)
    changes = compare(before, after)
    save('.guard.json', {'command_exit': code, 'allowed_outputs': allowed, 'changes': changes,
                         'status': 'mutated' if changes else 'unchanged'})
    if changes:
        print('REVIEWER MUTATED THE REPOSITORY: ' + str(prefix) + '.guard.json', file=sys.stderr)
        return 40
    release_artifacts(prefix)
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'review-guard: {error}; preserve artifacts', file=sys.stderr)
        sys.exit(2)
