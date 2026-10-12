import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.log = self.work / "commands.jsonl"
        archive = self.work / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as source:
            for name, content in {
                "go.mod": b"module example.com/substrate\n",
                "LICENSE": b"Apache License\n",
                "_LICENSES/example.com/dep/LICENSE": b"MIT License\n",
                "cmd/dataplane/envoy/Dockerfile": b"FROM envoy\n",
            }.items():
                info = tarfile.TarInfo(f"source/{name}")
                info.size = len(content)
                source.addfile(info, io.BytesIO(content))

        stub = self.work / "stub"
        stub.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import shutil
import sys

name = Path(sys.argv[0]).name
with open(os.environ["BUILD_LOG"], "a") as log:
    log.write(json.dumps([name, sys.argv[1:], os.environ.get("KO_DEFAULTPLATFORMS")]) + "\\n")
if name == "curl":
    shutil.copyfile(os.environ["BUILD_ARCHIVE"], sys.argv[sys.argv.index("--output") + 1])
if name in ("make", "ko"):
    images = [arg for arg in sys.argv if arg.startswith("IMAGES=")]
    images = images[0][7:].split() if images else ["./cmd/ate-setup"]
    for image in images:
        assert Path(image, "kodata/LICENSE").read_text() == "Apache License\\n"
        assert Path(image, "kodata/_LICENSES/example.com/dep/LICENSE").is_file()
if name == "ko":
    assert Path("cmd/ate-setup/kodata/go.mod").read_text().startswith("module ")
if name == "docker" and "cmd/dataplane/envoy" in sys.argv:
    context = sys.argv[sys.argv.index("--build-context") + 1].split("=", 1)[1]
    assert Path(context, "LICENSE").read_text() == "Apache License\\n"
    assert "COPY --from=substrate-license LICENSE" in Path("cmd/dataplane/envoy/Dockerfile").read_text()
""")
        stub.chmod(0o755)
        for command in ("curl", "make", "ko", "docker"):
            (self.work / command).symlink_to(stub)

        self.env = dict(os.environ, PATH=f"{self.work}:{os.environ['PATH']}",
                        BUILD_LOG=str(self.log), BUILD_ARCHIVE=str(archive))
        self.env.pop("SUBSTRATE_REF", None)
        self.env.pop("PLATFORMS", None)

    def build(self, *flags, tag="main"):
        result = subprocess.run(
            ["bash", str(ROOT / "build.sh"), *flags,
             "ghcr.io/example/substrate", f"ghcr.io/example/runner:{tag}"],
            env=self.env, text=True, capture_output=True,
        )
        return result

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_publish_all_images_with_one_tag(self):
        result = self.build(tag="v1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        curl, make, ko, envoy, runner = self.commands()
        self.assertIn((ROOT / "substrate.ref").read_text().strip(), " ".join(curl[1]))
        images = next(arg for arg in make[1] if arg.startswith("IMAGES="))
        self.assertEqual(len(images.split()), 6)
        self.assertIn("KO_FLAGS=--push=true", make[1])
        self.assertIn("KO_TAGS=--tags=v1.2.3", make[1])
        self.assertIn("--tags=v1.2.3", ko[1])
        self.assertIn("--push=true", ko[1])
        self.assertIn("./cmd/ate-setup", ko[1])
        for command in (envoy, runner):
            self.assertIn("--push", command[1])
            self.assertIn("linux/amd64,linux/arm64", command[1])
            self.assertTrue(command[1][command[1].index("--tag") + 1].endswith(":v1.2.3"))

    def test_pr_never_pushes_and_supports_release_override(self):
        self.env.update(SUBSTRATE_REF="v0.3.0", PLATFORMS="linux/amd64")
        result = self.build("--no-push")
        self.assertEqual(result.returncode, 0, result.stderr)
        curl, make, ko, envoy, runner = self.commands()
        self.assertIn("https://api.github.com/repos/agent-substrate/substrate/tarball/v0.3.0", curl[1])
        self.assertIn("KO_FLAGS=--push=false", make[1])
        self.assertIn("--push=false", ko[1])
        self.assertEqual(ko[2], "linux/amd64")
        for command in (envoy, runner):
            self.assertNotIn("--push", command[1])
            self.assertIn("--output=type=cacheonly", command[1])

    def test_invalid_input_stops_before_download(self):
        for tag in ("bad/tag", "bad tag", "a" * 129):
            with self.subTest(tag=tag):
                self.assertNotEqual(self.build(tag=tag).returncode, 0)
                self.assertFalse(self.log.exists())
        self.env["SUBSTRATE_REF"] = "../../bad"
        self.assertNotEqual(self.build().returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
