#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build"
LAYER_DIR="$BUILD_DIR/qscanner-layer"

echo "Building QScanner Lambda Layer..."

# Check if qscanner binary exists
if [ ! -f "$PROJECT_ROOT/scanner-lambda/qscanner" ]; then
    echo "ERROR: qscanner binary not found at scanner-lambda/qscanner"
    echo "Please download and extract the QScanner binary:"
    echo "  cd scanner-lambda"
    echo "  tar -xzf /path/to/qscanner.tar.gz"
    exit 1
fi

# Create build directory
mkdir -p "$LAYER_DIR/bin"
mkdir -p "$BUILD_DIR"

# Copy qscanner binary to layer structure
echo "Copying qscanner binary..."
cp "$PROJECT_ROOT/scanner-lambda/qscanner" "$LAYER_DIR/bin/qscanner"
chmod +x "$LAYER_DIR/bin/qscanner"

# Create ZIP file
echo "Creating layer ZIP file..."
cd "$LAYER_DIR"
zip -r "$BUILD_DIR/qscanner-layer.zip" .

cd "$PROJECT_ROOT"

# Get ZIP file size
LAYER_SIZE=$(du -h "$BUILD_DIR/qscanner-layer.zip" | cut -f1)
echo "Layer ZIP created: build/qscanner-layer.zip ($LAYER_SIZE)"

# Verify size is under 50MB
LAYER_SIZE_BYTES=$(stat -f%z "$BUILD_DIR/qscanner-layer.zip" 2>/dev/null || stat -c%s "$BUILD_DIR/qscanner-layer.zip")
LAYER_SIZE_MB=$((LAYER_SIZE_BYTES / 1024 / 1024))

if [ $LAYER_SIZE_MB -ge 50 ]; then
    echo "WARNING: Layer size is ${LAYER_SIZE_MB}MB, which exceeds Lambda's 50MB limit"
    echo "Consider using Docker deployment instead"
    exit 1
fi

echo "Success! Layer size: ${LAYER_SIZE_MB}MB (under 50MB limit)"
