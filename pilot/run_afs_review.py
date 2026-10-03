"""Run the installed AFS vision model in review-only rename mode.

The source is explicitly deny-written by the macOS sandbox. This command
never calls AFS apply or categorize operations.
"""

import json
import os
import subprocess
from pathlib import Path

from folder_studio import AB, SOURCE, STATE, check_source
from photo_workflow import find_photos


APP = Path('/Volumes/AI File Sorter for Mac M1/AI File Sorter for Mac M1.app/Contents/MacOS/AIFileSorter')


def main():
    root = check_source()
    if not APP.is_file():
        raise SystemExit('AI File Sorter DMG is not mounted')
    paths = [path for path in find_photos(root) if path.suffix.lower() in
             {'.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp'}]
    existing = {row['fileName'] for row in json.loads(
        (AB / 'aifs-gemma-review.json').read_text())['entries']}
    missing = [path for path in paths if path.name not in existing]
    if not missing:
        print('All images already have AFS review-only results')
        return
    STATE.mkdir(parents=True, exist_ok=True)
    profile = ('(version 1) (allow default) (deny network*) '
               f'(deny file-write* (subpath "{root}"))')
    command = ['sandbox-exec', '-p', profile, str(APP), '--headless',
               '--operation', 'rename', '--review-only']
    for path in missing:
        command += ['--path', str(path)]
    command += ['--status-file', str(STATE / 'afs-missing-status.json'),
                '--review-file', str(STATE / 'afs-missing-review.json'),
                '--settings-overrides-file', str(AB / 'rename-review-settings.json')]
    env = dict(os.environ, AI_FILE_SORTER_CONFIG_DIR=str(AB / 'config'),
               AI_FILE_SORTER_LLM_STORAGE_DIR=str(AB / 'models'),
               QT_QPA_PLATFORM='offscreen')
    print(f'AFS review-only analysis: {len(missing)} images', flush=True)
    completed = subprocess.run(command, env=env, timeout=1800)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    print('Saved:', STATE / 'afs-missing-review.json', flush=True)


if __name__ == '__main__':
    main()
