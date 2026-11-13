#!/usr/bin/env python3
"""Release redundant recovery data from a completed, unchanged guard attempt.

Without --apply, print only the eligible files. Never remove review results,
logs, guard reports, receipts, or artifacts from an uncertain/mutated attempt.
"""
import argparse
import json
from pathlib import Path


def redundant_artifacts(prefix):
    prefix = str(prefix)
    try:
        guard = json.loads(Path(prefix + '.guard.json').read_text())
        result = json.loads(Path(prefix + '.stdout').read_text())
        exit_code = int(Path(prefix + '.exit').read_text().strip())
        if guard.get('status') != 'unchanged' or guard.get('changes') != []:
            return []
        if result.get('native_host_execution') is True:
            handoff = json.loads(Path(prefix + '.handoff.json').read_text())
            receipt = json.loads(Path(prefix + '.receipt.json').read_text())
            if (exit_code != 0 or result.get('completion_verified') is not True
                    or receipt.get('handoff_id') != handoff.get('handoff_id')
                    or not handoff.get('handoff_id')
                    or receipt.get('status') != 'completed'
                    or receipt.get('completion_verified') is not True
                    or not receipt.get('host_task_id') or not receipt.get('completion_event')):
                return []
        elif (result.get('cleanup_verified') is not True
              or exit_code != guard.get('command_exit')):
            return []
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    return [path for suffix in ('.backup.tar', '.before.json', '.after.json')
            if (path := Path(prefix + suffix)).is_file() and not path.is_symlink()]


def release_artifacts(prefix):
    for path in redundant_artifacts(prefix):
        path.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefix', nargs='+', help='exact completed attempt prefix, not a directory')
    parser.add_argument('--apply', action='store_true', help='delete eligible recovery data')
    args = parser.parse_args()
    for prefix in args.prefix:
        for path in redundant_artifacts(prefix):
            print(path)
            if args.apply:
                path.unlink()
