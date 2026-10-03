"""The built /organize bundle must contain the large photo picker UI."""

import re
import os
import unittest
from pathlib import Path


BUILD = Path(os.environ.get('IMMICH_WEB_BUILD',
    '/Volumes/T7_1T/local-photo-studio/photo-studio-build/immich-web/static'))
GENERATED = Path(os.environ.get('IMMICH_WEB_GENERATED',
    '/Volumes/T7_1T/local-photo-studio/photo-studio-build/immich-web/.svelte-kit/generated/client/app.js'))


class OrganizeBundleTests(unittest.TestCase):
    def test_organize_client_bundle_uses_optional_large_image_picker(self):
        if not BUILD.is_dir() or not GENERATED.is_file():
            self.skipTest('local Immich web build is not present')
        match = re.search(r'"/\(user\)/organize":\s*\[(\d+)', GENERATED.read_text())
        self.assertIsNotNone(match, 'organize route is missing from the client manifest')
        node = match.group(1)
        bundles = list((BUILD / '_app/immutable/nodes').glob(f'{node}.*.js'))
        self.assertEqual(len(bundles), 1)
        page = bundles[0].read_text()
        self.assertIn('点图选择改名', page)
        self.assertIn('aria-pressed', page)
        self.assertIn('可选：批量改名', page)
        self.assertIn('h-64', page)
        self.assertNotIn('全选有可靠拍摄时间', page)

        source = (Path(__file__).resolve().parents[1] / 'vendor/immich-v3.2.2/web/src/routes/(user)/organize/+page.svelte').read_text()
        photo_picker = source.split('class={`grid gap-4 rounded-2xl border bg-white p-4 shadow-sm md:grid-cols-[256px_1fr] dark:bg-gray-900', 1)[1].split('</button>', 1)[0]
        self.assertNotIn('type="checkbox"', photo_picker)
        self.assertIn('点图选择改名', photo_picker)


if __name__ == '__main__':
    unittest.main()
