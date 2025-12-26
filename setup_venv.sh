#!/bin/bash
# Setup script for Suno Radio Bot
# This script creates a virtual environment and installs all requirements

echo "=== Suno Radio Bot Setup ==="
echo ""

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed. Please install Python 3 first."
    exit 1
fi

# Get Python version
PYTHON_VERSION=$(python3 --version)
echo "Found: $PYTHON_VERSION"
echo ""

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv venv

if [ $? -ne 0 ]; then
    echo "Error: Failed to create virtual environment."
    echo "You may need to install python3-venv package:"
    echo "  Ubuntu/Debian: sudo apt-get install python3-venv"
    echo "  CentOS/RHEL: sudo yum install python3-venv"
    exit 1
fi

echo "Virtual environment created successfully!"
echo ""

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo ""
echo "Installing requirements from requirements.txt..."
pip install -r requirements.txt

if [ $? -eq 0 ]; then
    echo ""
    echo "=== Setup Complete! ==="
    echo ""
    echo "To activate the virtual environment in the future, run:"
    echo "  source venv/bin/activate"
    echo ""
    echo "To deactivate when you're done, run:"
    echo "  deactivate"
else
    echo ""
    echo "Error: Failed to install some requirements."
    exit 1
fi



