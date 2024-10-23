import os
import json
import isodate
from typing import List, Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
from ratelimit import limits, sleep_and_retry

# Google/YouTube API related imports
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import youtube_transcript_api

# OpenAI and HTTP requests
import openai
import requests

@dataclass
class UnsplashImage:
    url: str
    alt: str

@dataclass
class VideoData:
    id: str
    title: str
    description: str
    published_at: str

def print_progress_separator(index: int, total: int, title: str) -> None:
    """
    Print a formatted progress separator with video information.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    progress = f"[{index}/{total}]"
    separator = f"{'=' * 20} {progress} {timestamp} {'=' * 20}"
    print(f"\n{separator}")
    print(f"Processing: {title}")
    print("=" * len(separator))

def load_config() -> Dict[str, Any]:
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

def get_video_transcript(video_id, language):
    try:
        transcript = youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id, languages=[language])
        return " ".join([entry["text"] for entry in transcript])
    except Exception as e:
        print(f"Error fetching transcript for video {video_id} in {language}: {e}")
        return None

def get_channel_videos(youtube, channel_id: str) -> List[VideoData]:
    """
    Retrieves all long-format videos (excluding Shorts) from a YouTube channel using pagination.
    Returns a list of VideoData objects.
    """
    videos = []
    next_page_token = None
    
    while True:
        try:
            # First get the video IDs and basic info
            request = youtube.search().list(
                part="id,snippet",
                channelId=channel_id,
                type="video",
                order="date",
                maxResults=50,  # Maximum allowed by YouTube API
                pageToken=next_page_token
            )
            
            response = request.execute()
            
            # Batch video IDs to get detailed information
            video_ids = [item["id"]["videoId"] for item in response.get("items", [])]
            
            if video_ids:
                # Get detailed video information including duration
                videos_request = youtube.videos().list(
                    part="contentDetails,snippet",
                    id=",".join(video_ids)
                )
                videos_response = videos_request.execute()
                
                for item in videos_response.get("items", []):
                    # Parse duration string (PT1H2M10S format)
                    duration_str = item["contentDetails"]["duration"]
                    
                    # Check if it's a Short video:
                    # 1. Duration is less than or equal to 60 seconds
                    # 2. Using vertical video aspect ratio (typically 9:16)
                    duration_seconds = parse_duration(duration_str)
                    
                    # Skip short video formats (<= 60s)
                    if duration_seconds <= 60:
                        continue
                        
                    video_data = VideoData(
                        id=item["id"],
                        title=item["snippet"]["title"],
                        description=item["snippet"]["description"],
                        published_at=item["snippet"]["publishedAt"]
                    )
                    videos.append(video_data)
            
            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break
                
        except Exception as e:
            print(f"Error fetching videos: {e}")
            break
    
    return videos

def parse_duration(duration_str: str) -> int:
    """
    Parse ISO 8601 duration format to seconds.
    Example: PT1H2M10S -> 3730 seconds
    """
    import re
    import isodate
    
    try:
        duration = isodate.parse_duration(duration_str)
        return int(duration.total_seconds())
    except Exception as e:
        print(f"Error parsing duration {duration_str}: {e}")
        return 0

def generate_article_from_transcript(transcript, title, source_language='fr'):
    openai.api_key = config['OPENAI_API_KEY']

    if source_language.lower() == 'fr':
        translation_instruction = "Translate the following French YouTube video transcript into English,"
    elif source_language.lower() == 'en':
        translation_instruction = "Translate the following YouTube video transcript and remove any promotional content, Subscribe to my channel, introductions, and conclusions,"
    else:
        translation_instruction = f"Translate the following {source_language} YouTube video transcript into English,"

    prompt = f"""{translation_instruction} removing filler sounds like "euh...", "bah", "ben", "hein" and similar French verbal tics.
    Rewrite it as a well-structured article in English, skipping the video introduction (e.g. Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), the ending (e.g. au revoir, à bientôt, ciao, N'oubliez pas de vous abonner, ...), and exclude any promotions, related to PIERREWRITER.COM, pier.com, pwrit.com and workshops.
    Ensure it reads well like an original article, not a transcript of a video, and can include personal ideas. Pay attention to French idioms and expressions, translating them to natural English equivalents.
    End the article with a call to action to engage the reader and read my Amazon book available in Kindle and paperback formats at https://book.ph7.me (use an anchor text like my book or "my latest published book").

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
    prompt = f"""Generate in English 5 relevant tags as a JSON array of strings for a Medium.com article titled "{title}".
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

def generate_medium_title(article_content: str) -> str:
    openai.api_key = config['OPENAI_API_KEY']
    prompt = f"""You are an expert content writer. Based on the content provided below, generate an engaging and clickable title for a Medium.com article.

    Content: {article_content[:1000]}  # Limit the content sent to the model
    
    Ensure the title grabs attention and would entice readers on Medium.com to click and read the story. The title should be creative and concise, ideally under 60 characters."""
    
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an expert content writer and title generator."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )
    
    return response.choices[0].message.content.strip('"')

def fetch_images_from_unsplash(query: str, per_page: int = 3) -> Optional[List[UnsplashImage]]:
    """
    Fetch black and white images from Unsplash API.
    Args:
        query: Search query for images
        per_page: Number of images to fetch (default: 3)
    """
    unsplash_access_key = config['UNSPLASH_ACCESS_KEY']
    url = (
        f"https://api.unsplash.com/search/photos"
        f"?query={query}"
        f"&client_id={unsplash_access_key}"
        f"&per_page={per_page}"
        f"&color=black_and_white"  # Only fetch black and white images https://unsplash.com/documentation#search-photos
    )

    try:
        response = requests.get(url)
        response.raise_for_status()
        results = response.json()['results']
        return [
            UnsplashImage(
                url=result['urls']['regular'],
                alt=f"{query} - Photo by {result['user']['name']} on Unsplash"
            ) 
            for result in results
        ]
    except Exception as e:
        print(f"Failed to fetch images from Unsplash: {e}")
        return None

def embed_images_in_content(article_content: str, images: List[UnsplashImage]) -> str:
    """
    Embed images in the article content using Markdown format.
    """
    if not images:
        return article_content

    # Split content into sections
    sections = article_content.split("\n\n")
    
    # Create image markdown with proper attribution
    image_blocks = []
    for image in images:
        # Use Markdown format for images
        image_md = f"\n![{image.alt}]({image.url})\n*{image.alt}*\n"
        image_blocks.append(image_md)

    # Distribute images throughout the content
    image_spacing = max(1, len(sections) // (len(images) + 1))
    for i, image_block in enumerate(image_blocks):
        insert_position = min((i + 1) * image_spacing, len(sections))
        sections.insert(insert_position, image_block)

    return "\n\n".join(sections)

def save_article_locally(original_title, title, tags, article):
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
    # os.makedirs('articles', exist_ok=True)

    # Create a safe filename from the title
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    file_name = os.path.join('articles', f"{safe_title}.md")

    # Check if article already exists
    if os.path.exists(file_name):
        # exit here if file already exists
        return file_name

    with open(file_name, "w", encoding="utf-8") as file:
        file.write(f"# {original_title}\n\n")
        file.write(f"Tags: {', '.join(tags)}\n\n")
        file.write(article)

    print(f"Article successfully saved locally: {file_name}")
    return file_name

@sleep_and_retry
@limits(calls=1, period=120) # 1 call for every 2 minutes
def post_to_medium(title: str, content: str, tags: List[str]) -> Optional[str]:
    """
    Post article to Medium with support for publication posting.
    """
    config = load_config()
    publication_id = config.get('MEDIUM_PUBLICATION_ID')
    post_to_publication = config.get('POST_TO_PUBLICATION', False)
    token = config['MEDIUM_ACCESS_TOKEN']

    # Prepare article in Markdown format
    full_content = f"# {title}\n\n{content}"

    article = {
        "title": title,
        "contentFormat": "markdown",
        "content": full_content,
        "tags": tags[:5],  # Medium allows up to 5 tags
        "publishStatus": "draft"
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Charset": "utf-8"
    }

    try:
        if post_to_publication and publication_id:
            # Post to publication
            response = requests.post(
                f"https://api.medium.com/v1/publications/{publication_id}/posts",
                headers=headers,
                json=article
            )
        else:
            # Post to user's profile
            # First get the user's id
            user_info = requests.get(
                "https://api.medium.com/v1/me",
                headers=headers
            )
            user_info.raise_for_status()
            user_id = user_info.json()['data']['id']

            # Then create the post under the user's profile
            response = requests.post(
                f"https://api.medium.com/v1/users/{user_id}/posts",
                headers=headers,
                json=article
            )

        response.raise_for_status()
        return response.json()["data"]["url"]

    except Exception as e:
        print(f"Failed to post article: {e}")
        print(f"Response: {response.text if 'response' in locals() else 'No response'}")
        return None

def check_article_exists(title: str) -> Optional[str]:
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    file_name = os.path.join('articles', f"{safe_title}.md")
    return file_name if os.path.exists(file_name) else None

def main():
    youtube = get_authenticated_service()
    channel_id = config['YOUTUBE_CHANNEL_ID']
    videos = get_channel_videos(youtube, channel_id)

    print(f"Found {len(videos)} videos in the channel.")

    source_language = config.get('SOURCE_LANGUAGE', 'fr')

    for index, video in enumerate(videos, 1):
        print_progress_separator(index, len(videos), video.title)
        
        # Skip if article already exists
        if check_article_exists(video.title):
            print(f"Article "{video.title}" already exists locally")
            continue

        try:
            transcript = get_video_transcript(video.id, language=source_language)
            if not transcript:
                print(f"No transcript available for: {video.title}")
                continue

            article = generate_article_from_transcript(transcript, video.title, source_language)
            tags = generate_tags(article, video.title)
            optimized_title = generate_medium_title(article)

            # Retrieve relevant images from Unsplash for the article
            images = fetch_images_from_unsplash(tags[0]) # Use first tag for image search
            if images:
                article = embed_images_in_content(article, images)

            # Save article locally
            save_article_locally(video.title, optimized_title, tags, article)

            # Post article to Medium
            medium_url = post_to_medium(optimized_title, article, tags)
            if medium_url:
                print(f"✓ Article posted to Medium: {medium_url}")
                print(f"✓ Generated tags added to the post: {tags}")
            else:
                print(f"✗ Failed to post article to Medium")

        except Exception as e:
            print(f"Error processing video {video.title}: {e}")

if __name__ == "__main__":
    main()
