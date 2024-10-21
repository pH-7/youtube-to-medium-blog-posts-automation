import os
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import youtube_transcript_api
import openai
import requests

# Set up your API keys and credentials
YOUTUBE_API_KEY = "AIzaSyAncSeXXZOxAxlVKoQinvBtCKbEUZFYZ1g"
OPENAI_API_KEY = "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"
MEDIUM_ACCESS_TOKEN = "2c904af77f880a2fd5724b2990dce6cae84c06adb8456d6cb1f0046e5beb01d06"

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

# Fetch videos from your YouTube channel
def get_channel_videos(youtube, channel_id):
    videos = []
    request = youtube.search().list(
        part="id,snippet",
        channelId=channel_id,
        type="video",
        order="date",
        maxResults=50
    )
    while request:
        response = request.execute()
        videos.extend(response["items"])
        request = youtube.search().list_next(request, response)
    return videos

# Transcribe video content
def transcribe_video(video_id):
    try:
        transcript = youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([entry["text"] for entry in transcript])
    except Exception as e:
        print(f"Error transcribing video {video_id}: {e}")
        return None

# Generate article from transcription
def generate_article(transcription, title):
    openai.api_key = OPENAI_API_KEY
    prompt = f"Write a well-structured article based on the following video transcription. Title: {title}\n\nTranscription: {transcription[:4000]}"
    
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a professional content writer."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=1500
    )
    
    return response.choices[0].message.content

# Post article to Medium
def post_to_medium(title, content, tags):
    url = "https://api.medium.com/v1/users/me/posts"
    headers = {
        "Authorization": f"Bearer {MEDIUM_ACCESS_TOKEN}",
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

def generate_tags(article_content, title):
    openai.api_key = OPENAI_API_KEY
    prompt = f"Generate 5 relevant tags for a Medium article with the following title and content. Provide the tags as a JSON array of strings.\n\nTitle: {title}\n\nContent: {article_content[:1000]}"
    
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that generates relevant tags for articles."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    
    try:
        tags = json.loads(response.choices[0].message.content)
        return tags if isinstance(tags, list) else []
    except json.JSONDecodeError:
        print("Error parsing tags. Using default tags.")
        return ["YouTube", "Content", "Article"]

def post_to_medium(title, content, tags):
    url = "https://api.medium.com/v1/users/me/posts"
    headers = {
        "Authorization": f"Bearer {MEDIUM_ACCESS_TOKEN}",
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
    channel_id = "UCXrntkhLN9WRV-kU8XymMYQ"
    videos = get_channel_videos(youtube, channel_id)
    
    for video in videos:
        video_id = video["id"]["videoId"]
        title = video["snippet"]["title"]
        
        transcription = transcribe_video(video_id)
        if transcription:
            article = generate_article(transcription, title)
            tags = generate_tags(article, title)
            medium_url = post_to_medium(title, article, tags)
            if medium_url:
                print(f"Article posted to Medium as a regular post: {medium_url}")
                print(f"Generated tags: {tags}")
                print("Note: This is not a Medium Story. To create a Story, you'll need to use the Medium mobile app.")

if __name__ == "__main__":
    main()