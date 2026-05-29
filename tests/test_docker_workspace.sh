#!/bin/bash
set -euo pipefail
# Test script to verify working directory configuration in Docker

echo "Testing Docker workspace configuration..."
echo "========================================="

# Test 1: Default (temp directory)
echo -e "\n1. Testing default configuration (isolated temp dir):"
docker run --rm \
  -v ~/.claude:/root/.claude \
  oh-my-gateway:test \
  python -c "from src.claude_cli import ClaudeCodeCLI; cli = ClaudeCodeCLI(); print(f'Working directory: {cli.cwd}'); print(f'Is temp dir: {cli.temp_dir is not None}')"

# Test 2: With an explicit working directory
echo -e "\n2. Testing with an explicit cwd:"
docker run --rm \
  -v ~/.claude:/root/.claude \
  oh-my-gateway:test \
  python -c "from src.claude_cli import ClaudeCodeCLI; cli = ClaudeCodeCLI(cwd='/app'); print(f'Working directory: {cli.cwd}'); print(f'Is temp dir: {cli.temp_dir is not None}')"

# Test 3: Per-user workspaces under USER_WORKSPACES_DIR
echo -e "\n3. Testing per-user workspace under USER_WORKSPACES_DIR:"
mkdir -p /tmp/test_workspace
docker run --rm \
  -v ~/.claude:/root/.claude \
  -v /tmp/test_workspace:/workspaces \
  -e USER_WORKSPACES_DIR=/workspaces \
  oh-my-gateway:test \
  python -c "import os; from src.workspace_manager import workspace_manager; ws = workspace_manager.resolve('alice', backend='claude'); print(f'Workspace: {ws}'); print(f'Directory exists: {os.path.exists(ws)}')"

echo -e "\n========================================="
echo "Docker workspace tests complete!"
