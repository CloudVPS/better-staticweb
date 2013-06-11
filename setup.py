#!/usr/bin/python

from setuptools import setup

setup(name='better_staticweb',
    version='1.0.0',
    description='An improved staticweb plugin for swift proxy',
    author='Koert van der Veer, CloudVPS',
    author_email='koert@cloudvps.com',
    url='https://github.com/CloudVPS/better-staticweb',
    py_modules=['staticweb'],
    requires=['swift(>=1.7)'],
    entry_points = {
        'paste.filter_factory': [
            'better_staticweb=better_staticweb:filter_factory',
        ]
    }
)
