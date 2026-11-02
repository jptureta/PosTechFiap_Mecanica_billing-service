# Orçamento e Pagamento (`billing-service`)

Microsserviço independente responsável pela geração e envio de orçamentos, aprovação/rejeição pelo cliente, integração com o **Mercado Pago** (geração de QR Code dinâmico in-store e webhook de confirmação) e compensação financeira (`REFUND_REQUIRED`) dentro da Saga distribuída.

---

## 1. Arquitetura e Comunicação Assíncrona (SQS)

- **Banco de Dados Exclusivo**: AWS RDS PostgreSQL 16 (`infra/main.tf`), gerenciado via migrações Alembic (`0001_initial`, `0002_mercado_pago`). Nenhum outro serviço acessa este banco.
- **Consumo de Comandos (`oficina-billing-commands-<env>.fifo` + DLQ)**:
  - `CreateQuote`: Cria orçamento de forma idempotente por `order_id`, dispara notificação ao cliente e publica `QuoteCreated` na fila `os-events.fifo`.
  - `RefundPayment`: Marca o pagamento como `REFUND_REQUIRED` (compensação da Saga) e publica `PaymentRefundRequested`.
- **Eventos Publicados (`oficina-os-events-<env>.fifo`)**: `QuoteCreated`, `QuoteApproved`, `QuoteRejected`, `PaymentConfirmed`, `PaymentFailed`, `PaymentRefundRequested`.

---

## 2. Endpoints e Especificação OpenAPI

A especificação OpenAPI completa está em [`openapi.json`](./openapi.json) e em `/docs`.

- `POST /quotes` / `GET /quotes/{id}` / `POST /quotes/{id}/send` — Criação, consulta e reenvio de orçamento.
- `POST /quotes/{id}/approval` e `POST /quotes/{id}/rejection` — Aprovação ou rejeição pelo cliente com emissão de evento para o orquestrador OS.
- `POST /quotes/{id}/payments` — Registra pagamento e, quando configurado com credenciais do Mercado Pago, gera QR Code dinâmico (`qr_data`).
- `POST /payments/{id}/verify` — Confirmação/recusa autenticada por `X-Provider-Token`.
- `POST /payments/mercado-pago/webhook` — Webhook para notificações `merchant_order` do Mercado Pago.
- `POST /internal/payments/{id}/compensate` — Solicitação de compensação/reembolso.
- `GET /health`, `GET /health/live`, `GET /metrics` — Healthchecks e métricas com `X-Correlation-ID`.

---

## 3. Testes, Cobertura e CI/CD

```bash
uv sync --dev
uv run pytest --cov=app --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

### Evidência de Cobertura de Testes (84% > 80% exigido)

```text
Name                   Stmts   Miss  Cover
------------------------------------------
app/__init__.py            0      0   100%
app/auth.py               26      0   100%
app/main.py              332     63    81%
app/messaging.py          58     13    78%
app/observability.py      58      1    98%
------------------------------------------
TOTAL                    474     77    84%
7 passed
```

O workflow `.github/workflows/ci-cd.yml` valida testes + cobertura >= 80%, executa scan no **SonarCloud**, valida o Terraform (`infra/`) e realiza deploy automatizado no **Amazon EKS**.
