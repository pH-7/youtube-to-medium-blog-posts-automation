import os
import json
from typing import List, Dict, Optional
from dataclasses import dataclass
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import youtube_transcript_api
import openai
import requests
from datetime import datetime, timedelta

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

def print_progress_separator(index: int, total: int, title: str):
    """
    Print a formatted progress separator with video information.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    progress = f"[{index}/{total}]"
    separator = f"{'=' * 20} {progress} {timestamp} {'=' * 20}"
    print(f"\n{separator}")
    print(f"Processing: {title}")
    print("=" * len(separator))

def load_config() -> dict:
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
    Retrieves ALL videos from a YouTube channel using pagination.
    Returns a list of VideoData objects.
    """
    videos = []
    next_page_token = None
    
    while True:
        try:
            request = youtube.search().list(
                part="id,snippet",
                channelId=channel_id,
                type="video",
                order="date",
                maxResults=50,  # Maximum allowed by YouTube API
                pageToken=next_page_token
            )
            
            response = request.execute()
            
            for item in response.get("items", []):
                video_data = VideoData(
                    id=item["id"]["videoId"],
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

def generate_article_from_transcript(transcript, title, source_language='fr'):
    openai.api_key = config['OPENAI_API_KEY']

    if source_language.lower() == 'fr':
        translation_instruction = "Translate the following French YouTube video transcript into English,"
    else:
        translation_instruction = f"Translate the following {source_language} YouTube video transcript into English,"

    prompt = f"""{translation_instruction} removing filler sounds like "euh...", "bah", "ben", "hein" and similar French verbal tics.
    Rewrite it as a well-structured article in English, skipping the video introduction (e.g. Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), the ending (e.g. au revoir, à bientôt, ciao, N'oubliez pas de vous abonner, ...), and any promotions related to PIERREWRITER.COM and my workshops.
    Ensure it reads well like an original article, not a transcript of a video, and can include personal ideas. Pay attention to French idioms and expressions, translating them to natural English equivalents.

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

def generate_medium_title(article_content):
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
    #if not os.path.exists('articles'):
    #  os.makedirs('articles')

    # Create a safe filename from the title
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    file_name = f"articles/{safe_title}.md"

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

def post_to_medium(title: str, content: str, tags: List[str]) -> Optional[str]:
    """
    Post article to Medium with improved error handling and validation.
    """
    token = config['MEDIUM_ACCESS_TOKEN']
    
    # Get user details
    try:
        user_info = requests.get(
            "https://api.medium.com/v1/me",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Charset": "utf-8"
            }
        )
        user_info.raise_for_status()
        user_id = user_info.json()['data']['id']
    except Exception as e:
        print(f"Error fetching user info: {e}")
        return None

    # Prepare article in Markdown format
    full_content = f"# {title}\n\n{content}"

    article = {
        "title": title,
        "contentFormat": "markdown",
        "content": full_content,
        "tags": tags[:5],  # Medium allows up to 5 tags
        "publishStatus": "draft"
    }

    try:
        response = requests.post(
            f"https://api.medium.com/v1/users/{user_id}/posts",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Charset": "utf-8"
            },
            json=article
        )
        response.raise_for_status()
        return response.json()["data"]["url"]
    except Exception as e:
        print(f"Failed to post article: {e}")
        print(f"Response: {response.text if 'response' in locals() else 'No response'}")
        return None

def main():
    youtube = get_authenticated_service()
    channel_id = config['YOUTUBE_CHANNEL_ID']
    videos = get_channel_videos(youtube, channel_id)

    print(f"Found {len(videos)} videos in the channel.")

    source_language = config.get('SOURCE_LANGUAGE', 'fr')
    
    # Create articles directory if it doesn't exist
    os.makedirs('articles', exist_ok=True)

    for index, video in enumerate(videos, 1):
        print_progress_separator(index, len(videos), video.title)
        
        # Skip if article already exists
        safe_title = "".join([c for c in video.title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
        file_name = f"articles/{safe_title}.md"
        
        if os.path.exists(file_name):
            print(f"Article already exists: {file_name}")
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
            local_file_path = save_article_locally(video.title, optimized_title, tags, article)

            # If filename already exists, it means it already exists so we skip that one
            if os.path.exists(local_file_path):
                print(f"Article already exists locally: {local_file_path}")
                continue

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
