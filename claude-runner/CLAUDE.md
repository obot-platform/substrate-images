# Obot environment

The user's Claude account was connected to Obot before this runner was launched.
Obot supplies the user's selected MCP servers and scoped credentials at session startup.
Use those MCP tools for the services they provide. No pairing command or pairing code is required.
If an Obot vMCP exposes `mmmcp_auth_status`, call it to connect the user's accounts. It requests browser authorization through URL elicitation, or returns sign-in links if elicitation is unavailable. Present any returned links as clickable links to the user. Let the user complete authorization in their browser; opening or accepting a link alone does not complete sign-in. After the user says they are done, call `mmmcp_auth_status` again to check authorization and refresh the available tools, then retry the original operation. Do not continuously poll or suspend/restart the session for this flow. Do not use shell requests to OAuth APIs, ask for API keys, or reset credentials. Report other connection errors without treating them as OAuth failures.

Claude's own connectors are separate from the MCP servers supplied by Obot; connecting a Claude connector does not repair an Obot MCP connection.
