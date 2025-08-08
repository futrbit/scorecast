from selenium import webdriver
from bs4 import BeautifulSoup
from datetime import datetime
import time

def scrape_football_news(teams=None):
    """
    Scrape football gossip from BBC Sport using Selenium.
    Args:
        teams (list): Optional list of team names to filter (e.g., ['Man Utd', 'Arsenal']).
    Returns:
        list: List of dicts with title, link, date, and source.
    """
    url = "https://www.bbc.co.uk/sport/football/gossip"
    try:
        print(f"Fetching {url}")
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124")
        driver = webdriver.Chrome(options=options)
        driver.get(url)
        time.sleep(3)  # Wait for initial load

        # Scroll to load more content
        for _ in range(2):  # Scroll twice for more articles
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        driver.quit()

        # Target top-level promo containers
        promo_items = soup.select("div[data-testid='promo'][type='article']")
        print(f"Found {len(promo_items)} article elements")

        articles = []
        seen_links = set()  # Track unique links to avoid duplicates
        for item in promo_items:
            print(f"\nProcessing item: {item.prettify()[:300]}...")
            title_elem = item.select_one("p[class*='PromoHeadline'], a")
            link_elem = item.select_one("a[href*='/sport/football/']")
            date_elem = item.select_one("time, span[class*='Date']")

            print(f"Title elem: {title_elem}")
            print(f"Link elem: {link_elem}")
            print(f"Date elem: {date_elem}")

            if title_elem and link_elem:
                title = title_elem.get_text(strip=True)
                link = link_elem.get("href")
                if not link.startswith("http"):
                    link = "https://www.bbc.co.uk" + link
                date = date_elem.get_text(strip=True) if date_elem else datetime.now().strftime("%Y-%m-%d")

                print(f"Extracted - Title: {title}, Link: {link}, Date: {date}")

                # Skip non-gossip or duplicates
                if title.lower() in ["football gossip", "gossip", ""] or link in seen_links:
                    continue
                seen_links.add(link)

                # Filter by teams if provided
                if teams:
                    # Check title and article content for team names
                    article_text = title.lower()
                    if any(team.lower() in article_text for team in teams):
                        articles.append({"title": title, "link": link, "date": date, "source": "BBC Sport"})
                    else:
                        # Optionally fetch article page for more content
                        try:
                            driver = webdriver.Chrome(options=options)
                            driver.get(link)
                            article_soup = BeautifulSoup(driver.page_source, "html.parser")
                            driver.quit()
                            content = article_soup.get_text(strip=True).lower()
                            if any(team.lower() in content for team in teams):
                                articles.append({"title": title, "link": link, "date": date, "source": "BBC Sport"})
                        except:
                            pass
                else:
                    articles.append({"title": title, "link": link, "date": date, "source": "BBC Sport"})

        print(f"Scraped {len(articles)} articles")
        return articles[:5]  # Limit to 5
    except Exception as e:
        print(f"Error: {e}")
        return []