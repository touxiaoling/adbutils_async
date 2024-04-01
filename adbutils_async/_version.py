#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Created on Fri May 06 2022 10:54:04 by codeskyblue"""

import pkg_resources


try:
    __version__ = pkg_resources.get_distribution("adbutils_async").version
except pkg_resources.DistributionNotFound:
    __version__ = "unknown"
