"""Translate AFS short labels locally; image files are not opened here."""

import json
import re
import urllib.request

from folder_studio import AFS_TRANSLATIONS, STATE, load_model_results, save_json


def translate(label):
    prompt = ('把这个英文照片文件名翻译成简短自然的简体中文名称。'
              '只翻译已有含义，不猜人物身份、地点或画面之外的信息。'
              '只输出中文名称，不要解释：' + label.replace('_', ' '))
    request = urllib.request.Request('http://127.0.0.1:11434/api/generate',
        data=json.dumps({'model': 'qwen3-translate:latest', 'prompt': prompt,
                         'stream': False, 'think': False,
                         'options': {'num_predict': 40, 'num_ctx': 2048}}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=180) as response:
        answer = json.load(response)['response'].strip().splitlines()[0].strip('"“” ')
        answer = re.sub(r'\s+', '', answer)
    if not re.fullmatch(r'[\u4e00-\u9fff\w，、·-]{2,30}', answer):
        raise ValueError('translation needs manual review: ' + answer)
    return answer


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    saved = json.loads(AFS_TRANSLATIONS.read_text()) if AFS_TRANSLATIONS.exists() else {}
    models = load_model_results()
    for filename, options in models.items():
        short = options.get('afs', {}).get('short')
        if not short or filename in saved:
            continue
        try:
            saved[filename] = translate(short)
            save_json(AFS_TRANSLATIONS, saved)
            print(f'{len(saved)}/{len(models)}: {filename} → {saved[filename]}', flush=True)
        except (ValueError, OSError) as error:
            print(f'{filename}: {error}', flush=True)


if __name__ == '__main__':
    main()
