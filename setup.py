from setuptools import setup


setup(
    name="logging-journald",
    version="0.6.2",
    author="Dmitry Orlov",
    author_email="me@mosquito.su",
    license="MIT",
    description=(
        "Pure python logging handler for writing logs to the journald "
        "using native protocol"
    ),
    long_description=open("README.rst").read(),
    platforms="all",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: Implementation :: CPython",
    ],
    packages=[""],
    python_requires=">=3.7, <4",
    url="https://github.com/mosquito/logging-journald",
    project_urls={
        "Source": "https://github.com/mosquito/logging-journald",
        "Tracker": "https://github.com/mosquito/logging-journald/issues",
    },
)
