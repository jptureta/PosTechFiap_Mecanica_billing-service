output "database_endpoint" {
  value = aws_db_instance.service.endpoint
}

output "database_security_group_id" {
  value = aws_security_group.service_db.id
}

output "billing_commands_queue_url" {
  value = aws_sqs_queue.billing_commands.url
}

output "billing_commands_dlq_url" {
  value = aws_sqs_queue.billing_commands_dlq.url
}
