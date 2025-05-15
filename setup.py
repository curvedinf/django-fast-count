from pathlib import Path

from setuptools import find_packages, setup

# The directory containing this file
HERE = Path(__file__).parent

# The text of the README file
README = (HERE / "README.md").read_text()

setup(
    name="django-fast-count",
    version="0.1.4",
    description="A fast Django .count() implementation for large tables.",
    long_description=README,
    long_description_content_type="text/markdown",
    url="https://github.com/curvedinf/django-fast-count",
    author="Chase Adams",
    author_email="chase.adams@gmail.com",
    license="MIT",
    packages=find_packages(),
    install_requires=[
        "django>=3.2",
    ],
    entry_points={
        "console_scripts": [
            "dir-assistant=dir_assistant.main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
)
