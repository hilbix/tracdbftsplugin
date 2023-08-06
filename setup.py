#! /usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2021 Jun Omae
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

from setuptools import setup, find_packages

def main():
    kwargs = {
        'name': 'TracDbftsPlugin',
        'version': '1.0.0.0',
        'description': 'Provide search feature using database fulltext index',
        'license': 'BSD',  # the same as Trac
        'url': 'http://trac-hacks.org/wiki/TracDbftsPlugin',
        'author': 'Jun Omae',
        'author_email': 'jun66j5@gmail.com',
        'install_requires': ['Trac'],
        'packages': find_packages(exclude=['*.tests*']),
        'entry_points': {
            'trac.plugins': [
                'tracdbfts.admin = tracdbfts.admin',
                'tracdbfts.api = tracdbfts.api',
                'tracdbfts.web_ui = tracdbfts.web_ui',
            ],
        },
    }
    setup(**kwargs)

if __name__ == '__main__':
    main()
