"""Read-only Apple Vision manifest adapter for virtual photo groups.

This is an independent adapter, not a copy of image-digger's source. It never
writes to the scanned folder and treats every classification as a suggestion.
"""

import json
import re
from pathlib import Path


SCREEN_NAME = re.compile(r'screenshot|screen[ _-]?shot|截屏|截图|屏幕快照', re.I)
GROUPS = ('截图／屏幕内容', '真人照片', '宠物与其他动物', '风景与建筑',
          '食物与物品', '动漫与插画', '海报与表情包', '文档与票据', '待确认／其他')


def classify_scan(row):
    """Return a reviewable, single primary group plus non-exclusive tags."""
    labels = {str(label.get('label', '')).lower(): float(label.get('confidence') or 0)
              for label in row.get('vision_labels', []) if isinstance(label, dict)}

    def strength(*words):
        return max((confidence for label, confidence in labels.items()
                    if any(word in label for word in words)), default=0)

    faces = int(row.get('face_count') or 0)
    humans = int(row.get('human_count') or 0)
    text_lines = int(row.get('ocr_line_count') or 0)
    people = max(faces, humans) > 0 or strength('person', 'people', 'crowd') >= 0.6
    tags = []
    if people:
        tags.append('有人')
        if max(faces, humans) >= 2:
            tags.append('多人')
        elif max(faces, humans) == 1:
            tags.append('单人')
    if text_lines >= 2:
        tags.append('有文字')

    category, reason, review = '待确认／其他', '线索不足', True
    if row.get('status') != 'scanned':
        reason = '图片未能完整分析'
    elif strength('screenshot') >= 0.65:
        category, reason, review = GROUPS[0], '视觉识别为屏幕内容', False
    elif SCREEN_NAME.search(str(row.get('filename') or '')):
        category, reason, review = GROUPS[0], '文件名提示为截图，需看图确认', True
    elif (str(row.get('extension') or '').lower() == 'png' and text_lines >= 8
          and not (row.get('camera_make') or row.get('camera_model'))):
        category, reason, review = GROUPS[0], '文字密集的无相机信息 PNG，疑似截图', True
    elif strength('document', 'receipt', 'invoice') >= 0.65 and text_lines >= 2:
        category, reason, review = GROUPS[7], '视觉文档线索与文字', False
    elif strength('anime', 'cartoon', 'illustration', 'drawing', 'comic') >= 0.65:
        category, reason, review = GROUPS[5], '视觉插画线索', False
    elif strength('poster', 'meme', 'graphic_design') >= 0.7:
        category, reason, review = GROUPS[6], '视觉海报线索', False
    elif people:
        category, reason, review = GROUPS[1], '检测到人物或人脸', False
    elif strength('cat', 'dog', 'animal', 'bird', 'pet', 'horse') >= 0.6:
        category, reason, review = GROUPS[2], '视觉动物线索', False
    elif strength('landscape', 'mountain', 'forest', 'beach', 'sky',
                  'building', 'architecture', 'city', 'street') >= 0.65:
        category, reason, review = GROUPS[3], '视觉风景或建筑线索', False
    elif strength('food', 'meal', 'fruit', 'drink', 'furniture',
                  'vehicle', 'clothing', 'product', 'object') >= 0.65:
        category, reason, review = GROUPS[4], '视觉物品线索', False
    if category == GROUPS[0]:
        tags.append('截图')
    return {'category': category, 'tags': tags, 'reason': reason,
            'needsReview': review, 'faceCount': faces, 'humanCount': humans,
            'ocrText': row.get('ocr_text_samples', [])[:8]}


def load_vision_results(root, inventory, manifest):
    """Attach only rows still matching this exact ordinary-folder inventory."""
    root = Path(root).resolve()
    manifest = Path(manifest)
    if not manifest.is_file():
        return {}
    by_path = {item['relativePath']: item for item in inventory}
    result = {}
    with manifest.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
                relative = Path(row['relative_path'])
                if relative.is_absolute() or '..' in relative.parts:
                    continue
                item = by_path.get(str(relative))
                if not item or Path(row['path']).resolve() != root / relative:
                    continue
                signature = item['signature']
                if (signature['size'] != row['file_size_bytes'] or
                        abs(signature['modifiedNs'] / 1e9 - row['file_modified_unix']) > 0.001):
                    continue
                result[str(relative)] = classify_scan(row)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return result
