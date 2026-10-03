import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
PROBE = ROOT / "apple_photokit_compile.swift"


class PhotoKitCompileTests(unittest.TestCase):
    def test_probe_is_typechecked_without_running_or_requesting_library_access(self):
        source = PROBE.read_text(encoding="utf-8")
        self.assertNotIn("@main", source)
        self.assertNotIn("PHPhotoLibrary.shared().performChanges", source)

        sdk = subprocess.run(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        result = subprocess.run(
            ["xcrun", "swiftc", "-typecheck", "-sdk", sdk, str(PROBE)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
