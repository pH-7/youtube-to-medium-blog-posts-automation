import os
import json
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import youtube_transcript_api
import openai
import requests

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

    Transcript: {transcript[:12000]}  # Increased transcript length to handle up to 5,000 words

    Structured article in English:"""
    
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
    prompt = f"""Generate 5 relevant English tags for a Medium article with the following title and content.
    Provide the tags as a JSON array of strings.

    Title: {title}

    Content: {article_content[:1000]}"""
    
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that generates relevant English tags for articles."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    
    try:
        tags = json.loads(response.choices[0].message.content)
        return tags if isinstance(tags, list) else []
    except json.JSONDecodeError:
        print("Error parsing tags. Using default tags.")
        default_tags = ["self-help", "psychology", "self-improvement"]
        return default_tags

def post_to_medium(title, content, tags):
    url = "https://api.medium.com/v1/users/me/posts"
    headers = {
        "Authorization": f"Bearer {config['MEDIUM_ACCESS_TOKEN']}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    data = {
        "title": title,
        "contentFormat": "html",
        "content": content,
        "tags": tags,
        "publishStatus": "draft"  # Change to "public" if you want to publish immediately
    }
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 201:
        print(f"Successfully posted article: {title}")
        return response.json()["data"]["url"]
    else:
        print(f"Failed to post article: {title}. Status code: {response.status_code}")
        return None

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