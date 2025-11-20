#!/bin/bash
# Script to create a Lambda Layer containing the QScanner binary

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Creating Lambda Layer for QScanner${NC}"

# Check if qscanner binary exists
if [ ! -f "qscanner" ]; then
    echo -e "${RED}Error: qscanner binary not found in current directory${NC}"
    echo "Please download the QScanner binary from Qualys and place it in this directory"
    exit 1
fi

# Make qscanner executable
chmod +x qscanner

# Create layer directory structure
echo -e "${YELLOW}Creating layer directory structure...${NC}"
mkdir -p layer/bin
cp qscanner layer/bin/

# Create zip file
echo -e "${YELLOW}Creating layer zip file...${NC}"
cd layer
zip -r ../qscanner-layer.zip .
cd ..

# Clean up
rm -rf layer

echo -e "${GREEN}Lambda Layer created: qscanner-layer.zip${NC}"
echo -e "${YELLOW}Layer size: $(du -h qscanner-layer.zip | cut -f1)${NC}"

# Check size limit (Lambda layers must be < 50MB compressed)
SIZE=$(stat -f%z qscanner-layer.zip 2>/dev/null || stat -c%s qscanner-layer.zip 2>/dev/null)
MAX_SIZE=$((50 * 1024 * 1024))

if [ $SIZE -gt $MAX_SIZE ]; then
    echo -e "${RED}Warning: Layer size exceeds 50MB limit. You must use Docker-based deployment.${NC}"
else
    echo -e "${GREEN}Layer size is within limits. You can deploy this as a Lambda Layer.${NC}"
fi

echo ""
echo "To publish this layer to AWS:"
echo "  aws lambda publish-layer-version \\"
echo "    --layer-name qscanner \\"
echo "    --description 'Qualys QScanner binary' \\"
echo "    --zip-file fileb://qscanner-layer.zip \\"
echo "    --compatible-runtimes python3.11 python3.12"
