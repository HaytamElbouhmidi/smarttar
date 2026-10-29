"""SmartTar v0.2.0 trusted native-binary launcher smoke test."""

import configparser
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from smarttar import inspect_archive, install_portable


C_SOURCE = r"""
/* SmartTar v0.2.0 relative-asset test fixture */
#include <stdio.h>

int main(void) {
    FILE *asset = fopen("asset.txt", "r");
    if (asset == NULL) {
        return 21;
    }
    char buffer[128];
    if (fgets(buffer, sizeof buffer, asset) == NULL) {
        fclose(asset);
        return 22;
    }
    fclose(asset);
    fputs(buffer, stdout);
    return 0;
}
"""


@unittest.skipUnless(shutil.which("cc"), "A C compiler is needed for the trusted native-binary test")
class PortableLaunchTests(unittest.TestCase):
    def test_installed_binary_loads_asset_using_launcher_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="smarttar-native-test-") as temporary:
            workspace = Path(temporary)
            source = workspace / "probe.c"
            binary = workspace / "probe"
            source.write_text(C_SOURCE, encoding="utf-8")
            subprocess.run(["cc", "-O2", str(source), "-o", str(binary)], check=True, capture_output=True)
            archive = workspace / "native-probe.tar.gz"
            with tarfile.open(archive, "w:gz") as stream:
                stream.add(binary, arcname="native/bin/probe")
                asset = b"SmartTar native launch verified\n"
                member = tarfile.TarInfo("native/bin/asset.txt")
                member.size = len(asset)
                member.mode = 0o644
                stream.addfile(member, io.BytesIO(asset))

            info = inspect_archive(archive)
            self.assertEqual(info.kind, "portable")
            self.assertEqual(info.binaries, ("native/bin/probe",))
            installed = install_portable(info, info.binaries[0], workspace / "apps", workspace / "launchers")
            executable = installed / "payload/native/bin/probe"
            desktop = configparser.ConfigParser(interpolation=None)
            desktop.read(workspace / "launchers/smarttar-native-probe.desktop", encoding="utf-8")
            working_directory = Path(desktop["Desktop Entry"]["Path"])
            self.assertEqual(working_directory, executable.parent)
            result = subprocess.run([str(executable)], cwd=working_directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "SmartTar native launch verified\n")


if __name__ == "__main__":
    unittest.main()
