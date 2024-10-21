import os
import json
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import youtube_transcript_api
import openai
import requests
from datetime import datetime, timedelta

# Function to load configuration
def load_config():
    with open('config.json', 'r') as config_file:
        return json.load(config_file)

# Load configuration
config = load_config()

# YouTube API setup
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

def get_authenticated_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def get_channel_videos(youtube, channel_id):
    videos = []
    next_page_token = None

    while True:
        request = youtube.search().list(
            part="id,snippet",
            channelId=channel_id,
            type="video",
            order="date",
            maxResults=50,
            pageToken=next_page_token
        )

        response = request.execute()
        videos.extend(response["items"])

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return videos

def get_video_transcript(video_id, language='fr'):
    try:
        transcript = youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id, languages=[language])
        return " ".join([entry["text"] for entry in transcript])
    except Exception as e:
        print(f"Error fetching transcript for video {video_id} in {language}: {e}")
        return None

def generate_article_from_transcript(transcript, title, source_language='fr'):
    openai.api_key = config['OPENAI_API_KEY']

    if source_language.lower() == 'fr':
        translation_instruction = "Translate the following French YouTube video transcript into English,"
    else:
        translation_instruction = f"Translate the following {source_language} YouTube video transcript into English,"

    prompt = f"""{translation_instruction} removing filler sounds like "euh...", "bah", "ben", "hein" and similar French verbal tics.
    Rewrite it as a well-structured article in English, skipping the video introduction (e.g. "Bonjour à tous", "Bienvenue sur ma chaîne", ...) and the ending (e.g. "au revoir", "à bientôt", "ciao", "N'oubliez pas de vous abonner", ...).
    Ensure it reads like an original article, not a transcript of a video. Pay attention to French idioms and expressions, translating them to natural English equivalents.

    Title: {title}

    Transcript: {transcript[:12000]}  # Increased transcript length to 1,2000 to handle up to 5,000 words

    Structured as a Medium.com article in English and use Markdown format for headings, links, bold, italic, etc:"""
    
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a professional translator, editor, and content writer."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=5000 # Increased max tokens to allow longer responses
    )
    
    return response.choices[0].message.content

def generate_tags(article_content, title):
    openai.api_key = config['OPENAI_API_KEY']
    prompt = f"""Generate 5 relevant tags as a JSON array of strings for an article titled "{title}".
    The article content is provided below:

    Content: {article_content[:1000]}"""
    
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an assistant that generates tags for Medium articles."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    
    try:
        tags = json.loads(response.choices[0].message.content)
        if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
            return tags
        else:
            print("Invalid tags generated. Using default tags.")
            return ["self-help", "psychology", "self-improvement"]
    except json.JSONDecodeError:
        print("Error parsing tags. Using default tags.")
        return ["self-help", "psychology", "self-improvement"]

def post_to_medium(title, content, tags):
    token = config['MEDIUM_ACCESS_TOKEN']
    # Get user details
    user_info = requests.get("https://api.medium.com/v1/me",
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json",
                                      "Accept": "application/json",
                                      "Accept-Charset": "utf-8"})
    if user_info.status_code != requests.codes.ok:
        print(f"Error fetching user info. Status code: {user_info.status_code}")
        return None

    user_id = user_info.json()['data']['id']

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Charset": "utf-8"
    }

    # Ensure content is not too long (Medium has a limit)
    max_content_length = 100000  # Medium's limit is around 100,000 characters
    if len(content) > max_content_length:
        content = content[:max_content_length-3] + "..."
        print(f"Content was truncated to {max_content_length} characters due to Medium's limit.")

    article = {
        "title": title,
        "contentFormat": "markdown",
        "content": content,
        "tags": tags[:5],  # Medium allows up to 5 tags
        "publishStatus": "draft"
    }

    response = requests.post(
        f"https://api.medium.com/v1/users/{user_id}/posts",
        headers=headers,
        json=article
    )

    if response.status_code == requests.codes.created:
        print(f"Successfully posted article: {title}")
        return response.json()["data"]["url"]
    else:
        print(f"Failed to post article: {title}. Status code: {response.status_code}")
        print(f"Response: {response.text}")
        print(f"Request payload: {json.dumps(article, indent=2)}")
        return None

def save_article_locally(title, tags, article):
    """
    Save the generated article locally as a Markdown file.

    Args:
    title (str): The title of the article.
    tags (list): List of tags for the article.
    article (str): The content of the article in Markdown format.

    Returns:
    str: The path of the saved file.
    """
    # Create 'articles' directory if it doesn't exist
    #if not os.path.exists('articles'):
    #  os.makedirs('articles')

    # Create a safe filename from the title
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    file_name = f"articles/{safe_title}.md"

    with open(file_name, "w", encoding="utf-8") as file:
        file.write(f"# {title}\n\n")
        file.write(f"Tags: {', '.join(tags)}\n\n")
        file.write(article)

    print(f"Article successfully created locally: {file_name}")
    return file_name

def main():
    youtube = get_authenticated_service()
    channel_id = config['YOUTUBE_CHANNEL_ID']
    videos = get_channel_videos(youtube, channel_id)

    print(f"Found {len(videos)} videos in the channel.")

    # Set source_language to French by default
    source_language = config.get('SOURCE_LANGUAGE', 'fr')

    for index, video in enumerate(videos, 1):
        video_id = video["id"]["videoId"]
        title = video["snippet"]["title"]

        print(f"Processing video {index}/{len(videos)}: {title}")

        transcript = get_video_transcript(video_id, language=source_language)
        if transcript:
            article = generate_article_from_transcript(transcript, title, source_language)
            tags = generate_tags(article, title)

            # Save article locally
            local_file_path = save_article_locally(title, tags, article)
            print(f"Article saved locally: {local_file_path}")

            medium_url = post_to_medium(title, article, tags)
            if medium_url:
                print(f"Article posted to Medium as a draft: {medium_url}")
                print(f"Generated tags: {tags}")
            else:
                print(f"Failed to post article for video: {title}")
        else:
            print(f"Failed to get transcript for video: {title}")

        print("--------------------")

if __name__ == "__main__":
    main()