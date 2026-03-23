#!/usr/bin/env bash
# Setup n8n workflows for audiobook pipeline
# Requires: N8N_API_KEY environment variable
# Usage: N8N_API_KEY=<key> ./setup.sh

set -euo pipefail

N8N_URL="${N8N_URL:-https://n8n.viktorbarzin.me}"
API_URL="${N8N_URL}/api/v1"

if [ -z "${N8N_API_KEY:-}" ]; then
  echo "Error: N8N_API_KEY not set"
  echo "Get it from n8n UI: Settings → API → Create API Key"
  exit 1
fi

AUTH_HEADER="X-N8N-API-KEY: ${N8N_API_KEY}"

echo "Creating Audiobook Search workflow..."
curl -s -X POST "${API_URL}/workflows" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  -d @- <<'SEARCH_EOF'
{
  "name": "Audiobook Search",
  "active": true,
  "nodes": [
    {
      "parameters": {
        "httpMethod": "POST",
        "path": "audiobook-search",
        "responseMode": "responseNode",
        "options": {}
      },
      "name": "Webhook",
      "type": "n8n-nodes-base.webhook",
      "typeVersion": 2,
      "position": [250, 300]
    },
    {
      "parameters": {
        "url": "=http://audiobook-search.servarr.svc.cluster.local/search?q={{ encodeURIComponent($json.body.title) }}",
        "options": {}
      },
      "name": "Search AudioBookBay",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.2,
      "position": [470, 300]
    },
    {
      "parameters": {
        "respondWith": "json",
        "responseBody": "={{ $json }}"
      },
      "name": "Respond to Webhook",
      "type": "n8n-nodes-base.respondToWebhook",
      "typeVersion": 1.1,
      "position": [690, 300]
    }
  ],
  "connections": {
    "Webhook": {
      "main": [
        [{"node": "Search AudioBookBay", "type": "main", "index": 0}]
      ]
    },
    "Search AudioBookBay": {
      "main": [
        [{"node": "Respond to Webhook", "type": "main", "index": 0}]
      ]
    }
  }
}
SEARCH_EOF
echo " Done."

echo "Creating Audiobook Download workflow..."
curl -s -X POST "${API_URL}/workflows" \
  -H "${AUTH_HEADER}" \
  -H "Content-Type: application/json" \
  -d @- <<'DOWNLOAD_EOF'
{
  "name": "Audiobook Download",
  "active": true,
  "nodes": [
    {
      "parameters": {
        "httpMethod": "POST",
        "path": "audiobook-download",
        "responseMode": "responseNode",
        "options": {}
      },
      "name": "Webhook",
      "type": "n8n-nodes-base.webhook",
      "typeVersion": 2,
      "position": [250, 300]
    },
    {
      "parameters": {
        "method": "POST",
        "url": "http://qbittorrent.servarr.svc.cluster.local/api/v2/torrents/add",
        "sendBody": true,
        "contentType": "form-urlencoded",
        "bodyParameters": {
          "parameters": [
            {"name": "urls", "value": "={{ $json.body.magnet_url }}"},
            {"name": "savepath", "value": "=/audiobooks/{{ $json.body.author }}/{{ $json.body.title }}"},
            {"name": "category", "value": "audiobooks"}
          ]
        },
        "options": {}
      },
      "name": "Add to qBittorrent",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.2,
      "position": [470, 300]
    },
    {
      "parameters": {
        "amount": 30,
        "unit": "seconds"
      },
      "name": "Wait 30s",
      "type": "n8n-nodes-base.wait",
      "typeVersion": 1.1,
      "position": [690, 300]
    },
    {
      "parameters": {
        "url": "http://qbittorrent.servarr.svc.cluster.local/api/v2/torrents/info",
        "options": {
          "queryParameters": {
            "parameters": [
              {"name": "category", "value": "audiobooks"},
              {"name": "sort", "value": "added_on"},
              {"name": "reverse", "value": "true"},
              {"name": "limit", "value": "1"}
            ]
          }
        }
      },
      "name": "Check Progress",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.2,
      "position": [910, 300]
    },
    {
      "parameters": {
        "method": "POST",
        "url": "http://audiobookshelf.audiobookshelf.svc.cluster.local/api/libraries/scan",
        "options": {}
      },
      "name": "Scan Audiobookshelf",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.2,
      "position": [1130, 300]
    },
    {
      "parameters": {
        "respondWith": "json",
        "responseBody": "={\"status\": \"ok\", \"message\": \"Download started and library scan triggered\"}"
      },
      "name": "Respond Success",
      "type": "n8n-nodes-base.respondToWebhook",
      "typeVersion": 1.1,
      "position": [690, 500]
    }
  ],
  "connections": {
    "Webhook": {
      "main": [
        [
          {"node": "Add to qBittorrent", "type": "main", "index": 0},
          {"node": "Respond Success", "type": "main", "index": 0}
        ]
      ]
    },
    "Add to qBittorrent": {
      "main": [
        [{"node": "Wait 30s", "type": "main", "index": 0}]
      ]
    },
    "Wait 30s": {
      "main": [
        [{"node": "Check Progress", "type": "main", "index": 0}]
      ]
    },
    "Check Progress": {
      "main": [
        [{"node": "Scan Audiobookshelf", "type": "main", "index": 0}]
      ]
    }
  }
}
DOWNLOAD_EOF
echo " Done."

echo ""
echo "Workflows created! Test with:"
echo "  curl -X POST ${N8N_URL}/webhook/audiobook-search -H 'Content-Type: application/json' -d '{\"title\": \"Project Hail Mary\"}'"
echo "  curl -X POST ${N8N_URL}/webhook/audiobook-download -H 'Content-Type: application/json' -d '{\"magnet_url\": \"magnet:...\", \"author\": \"Andy Weir\", \"title\": \"Project Hail Mary\"}'"
