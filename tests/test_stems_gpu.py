import os
import tempfile
import unittest
from unittest import mock

import stems


class GpuCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(stems, "GPU_PROC_DIR", os.path.join(self.tmp.name, "gpus")),
            mock.patch.object(stems, "_gpu_checked", (0.0, False)),
            mock.patch.object(stems, "_temp_checked", (0.0, None)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_no_driver_means_no_gpu_and_no_process_is_started(self):
        with mock.patch.object(stems.subprocess, "run", side_effect=AssertionError("nvidia-smi must not run")):
            self.assertFalse(stems.gpu_present())
            self.assertFalse(stems.gpu_ok())
            self.assertIsNone(stems.gpu_temperature())

    def test_a_bound_gpu_is_asked_through_nvidia_smi(self):
        os.makedirs(os.path.join(self.tmp.name, "gpus", "0000:01:00.0"))
        answers = iter([mock.Mock(returncode=0, stdout="GPU 0: Tesla P4"), mock.Mock(stdout="61\n")])
        with mock.patch.object(stems.subprocess, "run", side_effect=lambda *a, **k: next(answers)):
            self.assertTrue(stems.gpu_present())
            self.assertTrue(stems.gpu_ok())
            self.assertEqual(stems.gpu_temperature(), 61)

    def test_an_empty_gpu_folder_counts_as_no_gpu(self):
        os.makedirs(os.path.join(self.tmp.name, "gpus"))
        self.assertFalse(stems.gpu_present())

    def test_the_shortcut_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"DJ_STEMS_GPU_CHECK": "0"}):
            self.assertTrue(stems.gpu_present())


if __name__ == "__main__":
    unittest.main()
