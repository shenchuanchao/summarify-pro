"""
Vercel Serverless Entry Point
This file imports the Flask app from the parent directory for Vercel deployment.
"""

import sys
import os

# Add parent directory to path so we can import app.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

# Vercel expects the WSGI app to be exported as 'app'
