from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "executor.py"

extensions = [
    Extension(
        "app.executor",
        [str(SOURCE)],
        define_macros=[("NRUNMESH_OFFICIAL_ENGINE", "1")],
    ),
    Extension(
        "agent.engine_verifier",
        [str(ROOT / "agent" / "engine_verifier.py")],
        define_macros=[("NRUNMESH_OFFICIAL_VERIFIER", "1")],
    ),
]

setup(
    name="nrunmesh-agent-engine",
    version="0.1.0",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "binding": False,
            "embedsignature": False,
        },
        annotate=False,
    ),
)
