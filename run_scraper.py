import json
from scraper import scrape_football_news

# Scrape all news
news = scrape_football_news()
if news:
    with open("football_news.json", "w", encoding="utf-8") as f:
        json.dump(news, f, indent=2)
    print("News saved to football_news.json")
else:
    print("No articles scraped.")

# Scrape filtered news (e.g., specific teams)
teams = ["Man Utd", "Arsenal", "Chelsea"]  # Adjust as needed
filtered_news = scrape_football_news(teams=teams)
if filtered_news:
    with open("filtered_football_news.json", "w", encoding="utf-8") as f:
        json.dump(filtered_news, f, indent=2)
    print("Filtered news saved to filtered_football_news.json")
else:
    print("No filtered articles scraped.")