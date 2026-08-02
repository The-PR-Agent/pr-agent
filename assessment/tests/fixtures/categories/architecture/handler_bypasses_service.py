def update_account_handler(request, repository):
    account = repository.get(request.account_id)
    account.status = request.status
    repository.save(account)
    return account
