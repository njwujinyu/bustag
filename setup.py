import io
import os
import re

from setuptools import find_packages
from setuptools import setup
from bustag import __version__


def read(filename):
    filename = os.path.join(os.path.dirname(__file__), filename)
    text_type = type(u"")
    with io.open(filename, mode="r", encoding='utf-8') as fd:
        return re.sub(text_type(r':[a-z]+:`~?(.*?)`'), text_type(r'``\1``'), fd.read())


def read_requirements(filename):
    # requirements.txt 里除了空行和注释，每一行是一个依赖
    lines = [line.strip() for line in read(filename).splitlines()]
    return [line for line in lines if line and not line.startswith('#')]


setup(
    name="bustag",
    version=__version__,
    url="https://github.com/njwujinyu/bustag",
    license='MIT',

    author="gxtrobot",
    author_email="gxtrobot@gmail.com",

    description="a tag and recommend system for old bus driver",
    long_description=read("README.md"),
    long_description_content_type='text/markdown',

    packages=find_packages(exclude=('tests',)),
    package_data={'bustag': ['app/views/*.tpl', 'app/static/**/*']},

    python_requires='>=3.11',
    install_requires=read_requirements('requirements.txt'),

    entry_points={'console_scripts': ['bustag=bustag.main:main']},

    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
    ],
)
