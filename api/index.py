# -*- coding: utf-8 -*-
"""Vercel serverless entry: WSGI-обёртка вокруг Flask-приложения."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
