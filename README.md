# Orçamento e Pagamento

Serviço independente de orçamentos e registros de pagamento, com banco
PostgreSQL exclusivo. As APIs de pagamento ficam privadas no cluster; a
confirmação de um gateway é recebida em `POST /payments/{id}/verify` e protegida
por `PAYMENT_PROVIDER_TOKEN`.

Execute `docker compose up --build` e acesse `http://localhost:8002/docs`.
Para execução local fora do Docker, rode `alembic upgrade head` antes de iniciar
o servidor.
Testes e qualidade: `uv sync --dev && uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80 -q && uv run ruff check --select E4,E7,E9,F app tests`.

## API e Saga

- `POST /quotes` cria orçamento idempotente por `order_id`.
- `POST /quotes/{id}/approval` registra aprovação e publica `QuoteApproved` no RabbitMQ.
- `POST /quotes/{id}/payments` registra pagamento pendente.
- `POST /payments/{id}/verify` recebe confirmação/recusa assinada pelo gateway
  configurado e publica `PaymentConfirmed` ou `PaymentFailed` para o orquestrador OS.
- `/internal/payments/{id}/compensate` registra a necessidade de estorno após
  falha de reserva do estoque. Sem integração com um adquirente, o serviço não
  declara que o dinheiro foi devolvido: o pagamento fica `REFUND_REQUIRED` para
  reconciliação com o provedor.

Configure `OS_SERVICE_URL`, `INTERNAL_SERVICE_TOKEN` e `PAYMENT_PROVIDER_TOKEN`.
Use o mesmo token interno no OS e Inventory.
Configure `JWT_SECRET_KEY` e `JWT_ALGORITHM` para validar os mesmos tokens
assinados pela Lambda/API legada; clientes só acessam orçamentos próprios.
O namespace também precisa do secret `ghcr-pull` para leitura de imagens privadas
no GHCR.

Com `MESSAGING_ENABLED=true`, o serviço consome `CreateQuote` e `RefundPayment`
da fila durável `oficina.billing.commands` e publica os resultados em
`oficina.os.events`. Configure `RABBITMQ_URL`; em Kubernetes, o deployment lê
esse valor do secret `microservices-secrets`, chave `rabbitmq-url`. Sem mensageria,
as rotas internas REST continuam disponíveis para compatibilidade.
O Billing acessa somente seu banco PostgreSQL. O OS não acessa o banco do Billing.

O RDS exclusivo é definido em `infra/`. Valide com
`terraform -chdir=infra init -backend=false`, `terraform -chdir=infra validate`
e `terraform -chdir=infra plan` após fornecer as variáveis requeridas. O apply
exige backend de estado remoto e revisão do plano antes de provisionar recursos.
