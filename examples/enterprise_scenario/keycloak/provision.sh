#!/usr/bin/env bash
# Provisions the `oac` realm in a running Keycloak (26.2+) so the scenario's
# adapters can be exercised against a real, independently-implemented IdP.
#
# Every step here corresponds to a decision a platform team makes for real:
# which client represents the agent, which represents the gateway, which
# represents the downstream API, and which client is allowed to exchange tokens.
#
# Usage:
#   KC_HOME=/path/to/keycloak-26.x ./provision.sh [http://localhost:8380]
#
# Requires the Keycloak server to be running with admin/admin bootstrap
# credentials. Idempotent enough to re-run after `kcadm.sh delete realms/oac`.
set -euo pipefail

SERVER="${1:-http://localhost:8380}"
KC_HOME="${KC_HOME:?set KC_HOME to your Keycloak distribution directory}"
K="$KC_HOME/bin/kcadm.sh"

REALM=oac
GATEWAY_CLIENT_ID=openagent-control-gateway
GATEWAY_SECRET=gateway-secret
AGENT_CLIENT_ID=finance-invoice-svc
AGENT_SECRET=agent-secret
MCP_CLIENT_ID=finance-mcp-api
SCOPE=invoices:read

"$K" config credentials --server "$SERVER" --realm master --user admin --password admin
"$K" create realms -s realm=$REALM -s enabled=true

# The downstream API. Its client id IS the audience the MCP server validates.
"$K" create clients -r $REALM -s clientId=$MCP_CLIENT_ID -s enabled=true -s protocol=openid-connect

# The gateway: a confidential client permitted to perform RFC 8693 token
# exchange. This permission is the crux of the security model — an agent that
# had it could mint its own downstream credentials.
"$K" create clients -r $REALM -s clientId=$GATEWAY_CLIENT_ID -s enabled=true \
  -s publicClient=false -s serviceAccountsEnabled=true -s secret=$GATEWAY_SECRET \
  -s 'attributes."standard.token.exchange.enabled"=true'

# The agent workload.
"$K" create clients -r $REALM -s clientId=$AGENT_CLIENT_ID -s enabled=true \
  -s publicClient=false -s serviceAccountsEnabled=true -s secret=$AGENT_SECRET

AGENT_UUID=$("$K" get clients -r $REALM -q clientId=$AGENT_CLIENT_ID --fields id --format csv --noquotes | tail -1)
GATEWAY_UUID=$("$K" get clients -r $REALM -q clientId=$GATEWAY_CLIENT_ID --fields id --format csv --noquotes | tail -1)

# The agent's tokens must be audienced to the gateway, or the gateway rejects
# them (and a token audienced to the gateway is useless at the downstream API —
# that asymmetry is what makes bypassing the gateway impossible).
"$K" create clients/"$AGENT_UUID"/protocol-mappers/models -r $REALM \
  -s name=gateway-audience -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s "config.\"included.client.audience\"=$GATEWAY_CLIENT_ID" \
  -s 'config."access.token.claim"=true'

# The scope the MCP server requires, plus the audience mapper that makes
# finance-mcp-api a target Keycloak will actually issue exchanged tokens for.
"$K" create client-scopes -r $REALM -s name=$SCOPE -s protocol=openid-connect \
  -s 'attributes."include.in.token.scope"=true'
SCOPE_UUID=$("$K" get client-scopes -r $REALM --fields id,name --format csv --noquotes | grep "$SCOPE" | cut -d, -f1)

"$K" create client-scopes/"$SCOPE_UUID"/protocol-mappers/models -r $REALM \
  -s name=mcp-audience -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s "config.\"included.client.audience\"=$MCP_CLIENT_ID" \
  -s 'config."access.token.claim"=true'

# Assigned to the gateway (the exchanging client), so exchanged tokens carry it.
"$K" update clients/"$GATEWAY_UUID"/default-client-scopes/"$SCOPE_UUID" -r $REALM

echo
echo "Realm '$REALM' provisioned on $SERVER."
echo "Run the conformance tests with:"
echo "  OAC_TEST_KEYCLOAK_URL=$SERVER/realms/$REALM poetry run pytest tests/integration/test_keycloak_conformance.py"
