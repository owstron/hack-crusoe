from youdotcom import You
with You() as you:
    results = you.search(query="global birth rate trends", count=5)
    for result in results.results.web:
        print(result.title)
        print(result.url)
        if result.snippets:
            print(result.snippets[0])