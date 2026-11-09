# Feishu application bot receiver

Configure DEVFLOW_FEISHU_APP_ID and DEVFLOW_FEISHU_APP_SECRET in the ignored .env.
Run `docker compose up -d --build feishu`. The SDK maintains an outbound long connection;
no public callback URL is needed. Enable the application bot, subscribe to
im.message.receive_v1 using long connection, publish the application and add it to a group.

`docker compose logs --tail 20 feishu` shows sanitized connection status. Authenticated
websocket URLs and application credentials are redacted. User message bodies are not logged.
Messages are committed to PostgreSQL feishu_messages before acknowledgement, deduplicated
by message_id. Bot messages are ignored. Unbound messages do not trigger model or GitHub
operations. Project/chat bindings are persisted in feishu_bindings (application ID + group ID).
Only an authorized open_id explicitly mentioning the bound bot in a group text message
is routed as a Trigger to the project's Router. The same transaction stores
the raw message, conversation message and durable Trigger. Duplicate deliveries,
unknown groups, unauthorized senders and mentions of another bot cannot produce an admitted
Trigger. Binding does not replay earlier messages. Ordinary discussion is stored as a Trigger;
the project scope and admission table decide whether it becomes a candidate.
Router replies are stored in the bound API conversation and enqueue a feishu_reply
Effect in the same transaction. A notification worker replies to the original admitted message,
using an application tenant_access_token and stable Effect UUID for provider deduplication.
Both Router replies and terminal Router errors use this path. Destination bindings and
sender authorization are rechecked before sending. Failures retry up to five times; a retry
after 3500 seconds is stopped because provider UUID deduplication lasts one hour. Delivery is
not guaranteed exactly-once beyond that window. Historical context fetching, task-specific
reply correlation and discussion batching are not yet implemented.
Provision bindings with devflow.feishu_receiver.bind_group inside a database transaction;
no public binding endpoint is exposed yet.
The existing custom webhook remains the notification sender.


## Local verified binding

The group containing the explicit @DevFlow connectivity test is bound to
Xiangx520/DevFlow-test with one authorized sender (the test sender). The other observed
group remains unbound. Conversation ID: 0be097a8-c412-4931-af4d-60cc4e9a8f94.
The binding was checked in PostgreSQL; no historical test message was replayed and no
development task was created during provisioning. Real group-to-Router execution awaits
a new authorized @message. Regression: 38 passed, 2 optional integration tests skipped.


## Automatic reply validation (2026-11-07)

Backend regression: 44 passed, 2 optional integration tests skipped. Worker/API images updated.
Real reply test used the bound group's original connectivity-test message through the persistent
Effect queue. Token acquisition returned HTTP 200 with provider code 21001, "Access denied due
to platform policy". No reply was delivered; no development task was created. The user confirmed
a personal/Doubao identity. Similar inbound-working/outbound-blocked reports exist in
https://github.com/larksuite/openclaw-lark/issues/639 and https://github.com/larksuite/cli/issues/2815;
these reports do not constitute an official diagnosis for this application. Consult Feishu support
or the organization's administrator about application eligibility. Do not retry indefinitely or
claim live automatic replies passed. The failed test Effect remains available for inspection;
after official access is restored, send a fresh authorized @status message for end-to-end validation.
