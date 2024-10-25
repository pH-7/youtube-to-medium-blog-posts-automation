import os
import json
import isodate
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from datetime import datetime
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
    print(f"Processing Video: {title}")
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
    Current implementation analysis:
    1. Uses pagination (next_page_token) to iterate through all results
    2. Retrieves videos in batches of 50 (maximum allowed by YouTube API)
    3. Filters out short videos (<=60s)
    4. Gets detailed video information including duration

    Limitations:
    1. No error handling for API quotas
    2. No rate limiting implementation
    3. No handling for very large channels (potential timeout)

    Improved version:
    """
    @sleep_and_retry
    @limits(calls=1, period=1)  # Rate limit: 1 call per second
    def get_videos_page(youtube, uploads_playlist_id: str, page_token: Optional[str] = None):
        return youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token
        ).execute()

    videos = []
    next_page_token = None

    try:
        # Get uploads playlist ID
        channel_response = youtube.channels().list(
            part="contentDetails",
            id=channel_id
        ).execute()

        if not channel_response.get("items"):
            raise ValueError(f"No channel found for ID: {channel_id}")

        uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        while True:
            try:
                # Get videos from uploads playlist with rate limiting
                response = get_videos_page(youtube, uploads_playlist_id, next_page_token)

                # Get video IDs from playlist items
                video_ids = [item["snippet"]["resourceId"]["videoId"]
                             for item in response.get("items", [])]

                if video_ids:
                    # Get detailed video information in batches of 50
                    for i in range(0, len(video_ids), 50):
                        batch_ids = video_ids[i:i + 50]
                        videos_response = youtube.videos().list(
                            part="contentDetails,snippet",
                            id=",".join(batch_ids)
                        ).execute()

                        for item in videos_response.get("items", []):
                            duration_str = item["contentDetails"]["duration"]
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
                print(f"Error in pagination: {e}")
                # Wait before retrying or breaking
                time.sleep(5)
                if str(e).lower().find("quota") != -1:
                    print("YouTube API quota exceeded")
                    break
                continue

    except Exception as e:
        print(f"Error fetching videos: {e}")

    return videos

def parse_duration(duration_str: str) -> int:
    """
    Parse ISO 8601 duration format to seconds.
    Example: PT1H2M10S -> 3730 seconds
    """
    try:
        duration = isodate.parse_duration(duration_str)
        return int(duration.total_seconds())
    except Exception as e:
        print(f"Error parsing duration {duration_str}: {e}")
        return 0

def generate_article_from_transcript(transcript: str, title: str, source_language: str = 'fr', output_language: str = 'en') -> str:
    openai.api_key = config['OPENAI_API_KEY']

    # Define instructions and prompts for both English and French languages
    instructions = {
        'en': {
            'fr': "Translate the following French YouTube video transcript into English,",
            'en': "Translate the following YouTube video transcript and remove any promotional content, Subscribe to my channel, introductions, and conclusions,",
            'other': lambda lang: f"Translate the following {lang} YouTube video transcript into English,"
        },
        'fr': {
            'fr': "Reformule la transcription vidéo YouTube suivante en français,",
            'en': "Traduis la transcription vidéo YouTube suivante en français et supprime tout contenu promotionnel, les appels à s'abonner, les introductions et les conclusions,",
            'other': lambda lang: f"Traduis la transcription vidéo YouTube suivante du {lang} vers le français,"
        }
    }

    prompts = {
        'en': f"""{{instruction}} removing filler sounds like "euh...", "bah", "ben", "hein" and similar verbal tics.
    Rewrite it as a well-structured article in English, skipping the video introduction (e.g. Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), the ending (e.g. au revoir, à bientôt, ciao, N'oublie pas de t'abonner, ...), and exclude any promotions, related to PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com and workshops.
    Ensure it reads well like an original article, not a transcript of a video, and emphasise or highlight the personal ideas that would fascinate the readers. Pay attention to French idioms and expressions, translating them to natural English equivalents.
    End the article with a short bullet points recap, Actions List, or "Ask Yourself" questions preceded by a Markdown separator. Lastly, suggest readers to read my Amazon book at https://book.ph7.me (use an anchor text like my book or "my latest published book").

    Title: {title}

    Transcript: {transcript[:12000]}

    Structured as a Medium.com article in English and use Markdown format for headings, links, bold, italic, etc:""",

        'fr': f"""{{instruction}} en supprimant les sons de remplissage comme "euh...", "bah", "ben", "hein" et autres tics verbaux similaires.
    Réécris-le sous forme d'article bien structuré en français, en omettant l'introduction vidéo (ex: Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), la conclusion (ex: au revoir, à bientôt, ciao, N'oublie pas de t'abonner, ...), et exclus toute promotion liée à PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com et aux ateliers.
    Assure-toi que le texte se lit comme un véritable article, pas comme une transcription de vidéo, et mets en valeur les idées personnelles qui fascineraient les lecteurs. Porte une attention particulière aux expressions idiomatiques, en les adaptant naturellement en français.
    Termine l'article avec un bref récapitulatif sous forme de points et une liste d'actions précédé d'un Markdown séparateur. Enfin, suggère aux lecteurs de lire mon livre Amazon sur https://book.ph7.me (utilise un texte d'ancrage comme mon livre ou "mon dernier livre publié").

    Titre: {title}

    Transcription: {transcript[:12000]}

    Structuré comme un article Medium.com en français et utilise le format Markdown pour les titres, liens, gras, italique, etc:"""
    }

    # Get the appropriate instruction based on source and output languages
    instruction_map = instructions[output_language]
    if source_language.lower() in instruction_map:
        instruction = instruction_map[source_language.lower()]
    else:
        instruction = instruction_map['other'](source_language)

    # Get the appropriate prompt template and format it with the instruction
    prompt = prompts[output_language].format(instruction=instruction)

    # Set the system message based on output language
    system_messages = {
        'en': "You are a professional translator, editor, and content writer.",
        'fr': "Tu es un traducteur professionnel, éditeur et rédacteur de contenu."
    }

    response = openai.ChatCompletion.create(
        model=config['OPENAI_MODEL'],
        messages=[
            {"role": "system", "content": system_messages[output_language]},
            {"role": "user", "content": prompt}
        ],
        max_tokens=5000 # Increased max tokens to allow longer responses
    )

    return response.choices[0].message.content

def generate_tags(article_content: str, title: str, output_language: str = 'en') -> List[str]:
    """
    Generate tags for an article in either English or French.

    Args:
        article_content: The content of the article
        title: The title of the article
        output_language: Target language ('en' or 'fr')

    Returns:
        List[str]: List of exactly 5 tags in the specified language
    """
    openai.api_key = config['OPENAI_API_KEY']

    prompts = {
        'en': f'''Return ONLY a JSON array with exactly 5 tags for this article. The response must contain exactly 5 tags, no more, no less.
Title: "{title}"
Content: {article_content[:1000]}''',

        'fr': f'''Renvoie UNIQUEMENT un tableau JSON avec exactement 5 tags pour cet article. La réponse doit contenir exactement 5 tags, ni plus, ni moins.
Titre : "{title}"
Contenu : {article_content[:1000]}'''
    }

    system_messages = {
        'en': 'You are a tag generator. Only output JSON arrays with exactly 5 tags like ["tag1","tag2","tag3","tag4","tag5"]',
        'fr': 'Tu es un générateur de tags. Renvoie uniquement des tableaux JSON avec exactement 5 tags comme ["tag1","tag2","tag3","tag4","tag5"]'
    }

    # Default tags for each language
    default_tags = {
        'en': ["self help", "psychology", "self improvement", "personal development", "personal growth"],
        'fr': ["développement personnel", "psychologie", "croissance personnelle", "motivation", "bien-être"]
    }

    # Get the appropriate prompt and system message based on the output language
    prompt = prompts.get(output_language, prompts['en'])  # Default to English if language not found
    system_message = system_messages.get(output_language, system_messages['en'])

    try:
        response = openai.ChatCompletion.create(
            model=config['OPENAI_MODEL'],
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            max_tokens=100
        )

        # Clean and parse response
        content = response.choices[0].message.content.strip().strip('`')
        tags = json.loads(content)
        return tags[:5] if isinstance(tags, list) else default_tags[output_language]
    except (json.JSONDecodeError, Exception) as e:
        print(f"Error generating tags: {e}")
        return default_tags[output_language]

def generate_medium_title(article_content: str, output_language: str = 'en') -> str:
    """
    Generate an engaging title for Medium.com article in either English or French.

    Args:
        article_content: The content of the article
        output_language: Target language ('en' or 'fr')

    Returns:
        str: Generated title in the specified language
    """
    openai.api_key = config['OPENAI_API_KEY']

    prompts = {
        'en': f"""You are an expert content writer. Based on the content provided below, generate an engaging and clickable title for a Medium.com article.

    Content: {article_content[:1000]}  # Limit the content sent to the model
    
    Ensure the title grabs attention and would entice readers on Medium.com to click and read the story. The title should be creative and concise, ideally under 60 characters.
    Whenever possible, use one of the following formats: "How to [Action|Benefit] WITHOUT [Pain Point]?", "How to [Action|Benefit] in [Limited Time]?", or "The New Way to [Action] Without [Pain Point]?".

    Don't use irrelevant adjective like Unlock, Embrace, Unleash, Unmask, Unveil, Streamline, Fast-paced, Game-changer.""",

        'fr': f"""Tu es un expert en rédaction de contenu. À partir du contenu fourni ci-dessous, génère un titre accrocheur pour un article Medium.com.

    Contenu: {article_content[:1000]}  # Limite le contenu envoyé au modèle
    
    Assure-toi que le titre attire l'attention et donne envie aux lecteurs de Medium.com de cliquer et de lire l'histoire. Le titre doit être créatif et concis, idéalement moins de 60 caractères.
    Dans la mesure du possible, utilise l'un des formats suivants : "Comment [Action|Bénéfice] SANS [Point de Douleur] ?", "Comment [Action|Bénéfice] en [Temps Limité] ?", ou "La Nouvelle Façon de [Action] Sans [Point de Douleur] ?".

    N'utilise pas d'adjectifs non pertinents comme Débloquer, Embrasser, Dévoiler, Démasquer, Révéler, Rationaliser, Rapide, Révolutionnaire."""
    }

    system_messages = {
        'en': "You are an expert content writer and title generator.",
        'fr': "Tu es un expert en rédaction de contenu et en génération de titres."
    }

    # Use the appropriate prompt and system message based on the output language
    prompt = prompts.get(output_language, prompts['en'])  # Default to English if language not found
    system_message = system_messages.get(output_language, system_messages['en'])

    response = openai.ChatCompletion.create(
        model=config['OPENAI_MODEL'],
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ],
        max_tokens=100
    )

    return response.choices[0].message.content.strip('"')

def fetch_images_from_unsplash(query: str, per_page: int = 2) -> Optional[List[UnsplashImage]]:
    """
    Fetch images from Unsplash API, limited to 2 images maximum.
    Args:
        query: Search query for images
        per_page: Number of images to fetch (default: 2)
    Returns:
        Optional[List[UnsplashImage]]: List of UnsplashImage objects with URLs, alt text, and attribution captions in Markdown
    """
    unsplash_access_key = config['UNSPLASH_ACCESS_KEY']
    url = (
        f"https://api.unsplash.com/search/photos"
        f"?query={query}"
        f"&client_id={unsplash_access_key}"
        f"&per_page={per_page}"
    )

    try:
        response = requests.get(url)
        response.raise_for_status()
        results = response.json()['results']

        return [
            UnsplashImage(
                url=result['urls']['regular'],
                alt=result['description'] if result.get('description') else f"Photo by {result['user']['name']}",
                caption=f"Photo by [{result['user']['name']}]({result['user']['links']['html']}) on [{result['links']['html']}](Unsplash)"
            )
            for result in results
        ]
    except Exception as e:
        print(f"Failed to fetch images from Unsplash: {e}")
        return None

def embed_images_in_content(article_content: str, images: List[UnsplashImage], article_title: str) -> str:
    """
    Embed images in the article content using Markdown format with proper attribution captions.

    Args:
        article_content: The main article content
        images: List of UnsplashImage objects containing url, alt text, and caption
        article_title: Title of the article for image title attribute

    Returns:
        str: Article content with embedded images and their captions
    """
    if not images:
        return article_content

    # Split content into sections
    sections = article_content.split("\n\n")

    # Create image markdown blocks with attribution
    image_blocks = []
    for image in images:
        # Format: ![alt text](image_url "title")
        # Follow with caption in italics and attribution in regular text
        image_md = f"""\n![{image.alt}]({image.url} "{article_title}")
*{image.caption}*\n"""
        image_blocks.append(image_md)

    # For 2 images: place first image after first third, second image after second third
    # For 1 image: place it in the middle
    total_sections = len(sections)

    if len(images) == 2:
        first_pos = total_sections // 3
        second_pos = (total_sections * 2) // 3
        sections.insert(second_pos, image_blocks[1])
        sections.insert(first_pos, image_blocks[0])
    elif len(images) == 1:
        middle_pos = total_sections // 2
        sections.insert(middle_pos, image_blocks[0])

    return "\n\n".join(sections)

def save_article_locally(
        original_title: str,
        title: str,
        tags: List[str],
        article: str,
        medium_url: str,
        base_dir: str = 'articles'
) -> str:
    """
    Save the generated article locally as a Markdown file.

    Args:
        original_title (str): The original title from the video
        title (str): The optimized title for the article
        tags (List[str]): List of tags for the article
        article (str): The content of the article in Markdown format
        base_dir (str, optional): Base directory for saving articles. Defaults to 'articles'

    Returns:
        str: The path of the saved file

    Raises:
        OSError: If there are problems creating the directory or writing the file
        UnicodeEncodeError: If there are problems encoding the content
    """
    # Create 'articles' directory if it doesn't exist
    # os.makedirs('articles', exist_ok=True)

    # Create a safe filename from the original title
    safe_title: str = "".join([c for c in original_title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    file_name: str = os.path.join(base_dir, f"{safe_title}.md")

    # Create directory if it doesn't exist
    os.makedirs(base_dir, exist_ok=True)

    # Check if article already exists
    if os.path.exists(file_name):
        return file_name

    # Format tags with comma and space separation
    formatted_tags: str = ', '.join(tags)

    # Create Dev.to style frontmatter
    devto_frontmatter: str = f"""---
original_title: {original_title}
optimized_title: {title}
medium_url: {medium_url}
date: {datetime.now().isoformat()}
tags: {formatted_tags}
---

"""

    try:
        with open(file_name, "w", encoding="utf-8") as file:
            # Write frontmatter
            file.write(devto_frontmatter)
            # Write article content
            file.write(article)
    except (OSError, UnicodeEncodeError) as e:
        print(f"Error saving article: {e}")
        raise

    print(f"✓ Article successfully saved locally: {file_name}")
    return file_name

@sleep_and_retry
@limits(calls=1, period=120) # 1 call for every 2 minutes
def post_to_medium(title: str, content: str, tags: List[str], output_language: str) -> Optional[str]:
    """
    Post article to Medium with support for publication posting.
    """
    config = load_config()
    en_publication_id = config.get('MEDIUM_EN_PUBLICATION_ID')
    fr_publication_id = config.get('MEDIUM_FR_PUBLICATION_ID')
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
        if post_to_publication and (en_publication_id or fr_publication_id):
            publication_id = fr_publication_id if output_language == 'fr' else en_publication_id
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

    print(f"Found {len(videos)} videos in the channel")

    source_language = config.get('SOURCE_LANGUAGE', 'fr')
    output_language = config.get('OUTPUT_LANGUAGE', 'en')

    for index, video in enumerate(videos, 1):
        print_progress_separator(index, len(videos), video.title)

        # Skip if article already exists
        if check_article_exists(video.title):
            print(f"Already exists locally. Skipping '{video.title}'")
            continue

        try:
            transcript = get_video_transcript(video.id, language=source_language)
            if not transcript:
                print(f"No transcript available for: {video.title}")
                continue

            article = generate_article_from_transcript(
                transcript,
                video.title,
                source_language=source_language,
                output_language=output_language
            )
            tags = generate_tags(article, video.title, output_language=output_language)
            optimized_title = generate_medium_title(article)

            # Retrieve relevant images from Unsplash for the article
            images = fetch_images_from_unsplash(tags[0]) # Use first tag for image search
            if images:
                article = embed_images_in_content(article, images, optimized_title)

            # First, post article to Medium
            medium_url = post_to_medium(optimized_title, article, tags, output_language)

            # Second, save article locally too
            path_saved_file = save_article_locally(
                video.title,
                optimized_title,
                tags,
                article,
                medium_url
            )

            if medium_url and path_saved_file:
                print(f"✓ Article posted to Medium: {medium_url}")
                print(f"✓ Generated tags added to the post: {tags}")
            else:
                print(f"✗ Failed to post article to Medium")

        except Exception as e:
            print(f"Error processing video {video.title}: {e}")

if __name__ == "__main__":
    main()
