"""py2app packaging configuration for building standalone AppJuggler.app on macOS."""

from py2app.build_app import py2app as _py2app
from setuptools import setup


class CleanPy2App(_py2app):
    """Compatibility bridge for py2app 0.28 + modern setuptools PEP 621 pyproject.toml."""

    def finalize_options(self) -> None:
        # Modern setuptools auto-populates install_requires from pyproject.toml [project.dependencies],
        # which py2app 0.28 rejects because dependencies are already installed in the build venv.
        self.distribution.install_requires = []
        super().finalize_options()


APP = ["src/appjuggler/__main__.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "src/appjuggler/assets/AppJuggler.icns",
    "packages": ["appjuggler"],
    "plist": {
        "CFBundleName": "AppJuggler",
        "CFBundleDisplayName": "AppJuggler",
        "CFBundleIdentifier": "io.appjuggler.AppJuggler",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "13.0",
        # LSUIElement=True makes AppJuggler a clean menu-bar agent without a Dock tile
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSAccessibilityUsageDescription": (
            "AppJuggler uses Accessibility to enumerate application windows, raise selected "
            "windows, and listen for the double-Caps Lock keyboard navigation trigger."
        ),
    },
}

setup(
    name="AppJuggler",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    cmdclass={"py2app": CleanPy2App},
    package_dir={"": "src"},
)
