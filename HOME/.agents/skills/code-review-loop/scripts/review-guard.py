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
import stat
import subprocess
import sys
import tarfile


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


def tree(root, allowed, tracked):
    result = {}
    def visit(directory):
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if rel == '.git':
                continue
            excluded = any(rel == p or rel.startswith(p + '/') for p in allowed)
            protected = rel in tracked or any(t.startswith(rel + '/') for t in tracked)
            if excluded and not protected:
                continue
            result[rel] = record(path)
            if result[rel][0] == 'dir':
                visit(path)
    visit(root)
    return result


def metadata(root):
    # Git paths can live outside the working directory in a linked worktree.
    names = ['HEAD', 'index', 'refs', 'config', 'config.worktree', 'packed-refs',
             'MERGE_HEAD', 'MERGE_MSG', 'MERGE_MODE', 'ORIG_HEAD',
             'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'AUTO_MERGE',
             'rebase-merge', 'rebase-apply', 'sequencer', 'info/sparse-checkout']
    index = Path(os.fsdecode(git(root, 'rev-parse', '--git-path', 'index')).strip())
    if not index.is_absolute():
        index = root / index
    names.extend(path.name for path in sorted(index.parent.glob('sharedindex.*')))
    result, paths = {}, {}
    for name in names:
        path = Path(os.fsdecode(git(root, 'rev-parse', '--git-path', name)).strip())
        if not path.is_absolute():
            path = root / path
        paths[name] = path
        if path.exists() or path.is_symlink():
            result[name] = record(path)
            if path.is_dir() and not path.is_symlink():
                for child in sorted(path.rglob('*')):
                    result[name + '/' + child.relative_to(path).as_posix()] = record(child)
    marker = root / '.git'
    if marker.is_file():
        result['git-marker'] = record(marker)
    result['refs-list'] = os.fsdecode(git(root, 'for-each-ref', '--format=%(refname) %(objectname) %(symref)'))
    return result, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--stdin', required=True, help='prompt file')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if Path(os.fsdecode(git(root, 'rev-parse', '--show-toplevel')).strip()).resolve() != root:
        raise ValueError('--root must be the repository root')
    if any(entry.startswith(b'160000 ') for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0')):
        raise ValueError('submodules need separate guards; this runner does not support them')
    prefix = Path(args.prefix).resolve()
    if prefix.is_relative_to(root):
        raise ValueError('artifacts must live outside the repository')
    prefix.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        raise ValueError('missing reviewer command')
    allowed = json.loads(os.environ.get('REVIEW_OUTPUT_PATHS', '[]'))
    if not isinstance(allowed, list) or any(not isinstance(p, str) or not p or
            Path(p).is_absolute() or any(c in ('', '.', '..', '.git') for c in p.split('/')) for p in allowed):
        raise ValueError('REVIEW_OUTPUT_PATHS must be a JSON array of relative directory paths without .git or traversal')
    tracked = set(os.fsdecode(p) for p in git(root, 'ls-files', '-z').split(b'\0') if p)
    before = {'tree': tree(root, allowed, tracked), 'git': metadata(root)[0]}
    def save(suffix, data):
        path = Path(str(prefix) + suffix)
        with path.open('x', encoding='utf-8') as f:
            os.chmod(path, 0o600)
            json.dump(data, f, indent=2, ensure_ascii=True)
    save('.before.json', before)
    # A private archive contains all protected files, including ignored files.
    archive_path = Path(str(prefix) + '.backup.tar')
    with archive_path.open('xb') as output:
        os.chmod(archive_path, 0o600)
        with tarfile.open(fileobj=output, mode='w', dereference=False) as archive:
            for rel in before['tree']:
                archive.add(root / rel, arcname='tree/' + rel, recursive=False)
            for name, path in metadata(root)[1].items():
                if path.exists() or path.is_symlink():
                    archive.add(path, arcname='git/' + name, recursive=True)
            marker = root / '.git'
            if marker.is_file():
                archive.add(marker, arcname='git-marker', recursive=False)
    # Detect a concurrent edit during backup instead of launching on mixed inputs.
    if before != {'tree': tree(root, allowed, tracked), 'git': metadata(root)[0]}:
        raise ValueError('repository changed during backup; preserve artifacts and retry after edits stop')
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
    after = {'tree': tree(root, allowed, tracked), 'git': metadata(root)[0]}
    save('.after.json', after)
    changes = []
    for area in before:
        for key in sorted(before[area].keys() | after[area].keys()):
            if before[area].get(key) != after[area].get(key):
                changes.append(area + ':' + key)
    save('.guard.json', {'command_exit': code, 'allowed_outputs': allowed, 'changes': changes,
                         'status': 'mutated' if changes else 'unchanged'})
    if changes:
        print('REVIEWER MUTATED THE REPOSITORY: ' + str(prefix) + '.guard.json', file=sys.stderr)
        return 40
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'review-guard: {error}; preserve artifacts', file=sys.stderr)
        sys.exit(2)
