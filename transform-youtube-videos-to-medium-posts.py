import os
import json
import time
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

RATE_LIMIT_PERIOD_SECONDS = 300
MAX_CALLS_IN_PERIOD = 1
LONG_ARTICLE_THRESHOLD = 2499

@dataclass
class UnsplashImage:
    url: str
    alt: str
    caption: str

@dataclass
class VideoData:
    id: str
    title: str
    description: str
    published_at: str

@sleep_and_retry
@limits(calls=MAX_CALLS_IN_PERIOD, period=RATE_LIMIT_PERIOD_SECONDS) # 1 call for every 3 minutes
def print_progress_separator(index: int, total: int, title: str) -> None:
    """
    Print a formatted progress separator with video information.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    progress = f"[{index}/{total}]"
    separator = f"{'=' * 20} {progress} {timestamp} {'=' * 20}"
    print(f"\n{separator}")
    print(f"Start Processing: {title}")
    print("=" * len(separator))

def load_config() -> Dict[str, Any]:
    """
    Load configuration from config.json file.

    Returns:
        Dict[str, Any]: Configuration dictionary
    """
    with open('config.json', 'r') as config_file:
        return json.load(config_file)

# Load configuration
config = load_config()

# YouTube API setup
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

def get_authenticated_service():
    """
    Get authenticated YouTube service.

    Returns:
        googleapiclient.discovery.Resource: Authenticated YouTube service
    """
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w", encoding='utf-8') as token:  # Added encoding parameter
            token.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def get_video_transcript(video_id: str, language: str) -> Optional[str]:
    """
    Get video transcript in specified language. If not available directly,
    fetch auto-generated transcript and translate it.

    Args:
        video_id: YouTube video ID
        language: Language code (e.g., 'en', 'fr')

    Returns:
        Optional[str]: Combined transcript text or None if not available
    """
    try:
        transcript_list = youtube_transcript_api.YouTubeTranscriptApi.list_transcripts(video_id)

        try:
            # First try to get transcript in requested language
            transcript = transcript_list.find_transcript([language])
            print(f"✓ Found direct transcript in {language}")
        except Exception:
            try:
                # If not found, get auto-generated French transcript and translate it
                transcript = transcript_list.find_generated_transcript(['fr'])
                if language != 'fr':  # Only translate if target language is not French
                    transcript = transcript.translate(language)
                    print(f"✓ Using translated transcript from French to {language}")
                else:
                    print("✓ Using French auto-generated transcript")
            except Exception as e:
                print(f"✗ No transcript available in any format: {e}")
                return None

        text = transcript.fetch()
        return " ".join([entry["text"] for entry in text])

    except Exception as e:
        print(f"✗ Error fetching transcript for video {video_id} in {language}: {e}")
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

    Args:
        youtube: YouTube API service instance
        channel_id: ID of the YouTube channel

    Returns:
        List[VideoData]: List of published video data
    """
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
        # Get uploads playlist ID for the channel
        channel_response = youtube.channels().list(
            part="contentDetails",
            id=channel_id
        ).execute()

        if not channel_response.get("items"):
            raise ValueError(f"No channel found for ID: {channel_id}")

        uploads_playlist_id = channel_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        while True:
            try:
                response = get_videos_page(youtube, uploads_playlist_id, next_page_token)

                video_ids = [
                    item["snippet"]["resourceId"]["videoId"]
                    for item in response.get("items", [])
                    if item["snippet"].get("publishedAt")
                ]

                if video_ids:
                    # Get detailed video information in batches of 50
                    for i in range(0, len(video_ids), 50):
                        batch_ids = video_ids[i:i + 50]
                        videos_response = youtube.videos().list(
                            part="contentDetails,snippet,status",
                            id=",".join(batch_ids)
                        ).execute()

                        for item in videos_response.get("items", []):
                            # Only process public videos. Skip the non-public ones
                            if item["status"]["privacyStatus"] != "public":
                                continue

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

    # Sort videos by publish date, newest first
    videos.sort(key=lambda x: x.published_at, reverse=True)
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
    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])

    # Define instructions and prompts for both English and French languages
    instructions = {
        'en': {
            'fr': "Translate the following French YouTube video transcript into English,",
            'en': "Translate the following YouTube video transcript and remove any promotional content, 'Subscribe to my channel', introductions, and conclusions,",
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
    End the article with short bullet points TL;DR, Actions List, and/or "Ask Yourself" / "What About You ?" styled questions in italic font preceded by Markdown separator.
    Lastly, in the exact same personal voice tone as the transcript, lead readers to read my complementary Amazon book at https://book.ph7.me (use anchor text like "my new book" and emphasize/bold it), and/or suggest my podcast https://podcasts.ph7.me, and/or invite them subscribe to my private mailing list at https://masterclass.ph7.me (always use anchor text for links), preceded by another Markdown separator.

    Kicker: Right before Title, optional very short article kicker text in h3 font.
    Title: {title}
    Subtitle: Right after Title, optional concise appealing (spoiler) subtitle in h3 font.

    Transcript: {transcript[:12000]}

    Structured as a Medium.com article in English while keeping the exact same voice tone as in the original transcript.
    Use simple words, and DO NOT use any irrelevant or complicated adjective such as: Unlock, Effortless, Explore, Insights, Today's Digital World, In today's world, Dive into, Refine, Evolving, Embrace, Embracing, Embark, Enrich, Envision, Unleash, Unmask, Unveil, Streamline, Fast-paced, Delve, Digital Age, Game-changer, Indulge, Merely, Endure.
    Use Markdown format for headings, links, bold, italic, etc:""",

        'fr': f"""{{instruction}} en supprimant les sons de remplissage comme "euh...", "bah", "ben", "hein" et autres tics verbaux similaires.
    Réécris-le sous forme d'article bien structuré en français, en omettant l'introduction vidéo (ex: Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), la conclusion (ex: au revoir, à bientôt, ciao, N'oublie pas de t'abonner, ...), et exclus toute promotion liée à PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com et aux ateliers.
    Rédige la transcription vidéo sous forme d'un article facile à lire. Mets en valeur les idées personnelles qui peuvent fasciner.

    Si le texte le permet, utilise la structure suivante, MAIS en incorporant cette structure au texte de manière naturelle pour que cela ne soit pas évident pour le lecteur.
    1. Annonce / Présentation de la lecture
    (Dans les lignes qui suivent, vous allez apprendre comment...)
    2. Problèmes
    (Vous en avez assez de... ?)
    3. Fausses solutions
    (Vous avez peut-être essayé de X ou Y... mais...)
    4 Théorie / L'explication
    (La méthode dont je vais vous parler, elle consiste à...)
    5. Preuve / Exemple
    (Voici comment j'ai utilisé cette méthode...)
    6. En pratique / Mode d'emploi
    (Liste d'étapes concrètes pour faire la même chose chez vous)
    7. Étendre - Aller plus loin
    (Amener le lecteur au livre complémentaire https://livre.ph7.me (utilise un texte d'ancrage comme "mon livre" ou "mon dernier livre" et met le lien en gras), ou invite le lecteur à ma chaîne YouTube https://fr-youtube.ph7.me ou sur mon podcast https://podcast.ph7.me (utiliser texte d'ancrage).

    Termine l'article avec un bref récap sous forme de points et/ou liste d'actions que le lecteur peut directement appliquer, précédé d'un séparateur Markdown.
    Enfin, suggérer le lecteur de s'inscrire à ma liste de contacts sur https://contacts.ph7.me (utilise un texte d'ancrage), précédé d'un séparateur Markdown.

    Kicker: Juste avant le Titre, très courte phrase d'accroche optionnelle en police h3.
    Titre: {title}
    Sous-titre: Juste après le Titre, sous-titre optionnel en police h3, qui donne une promesse concise qui aguiche/intrigue davantage.

    Transcription: {transcript[:12000]}

    Structure le texte en tant qu'article Medium.com français tout en gardant exactement le même ton de voix que dans la transcription originale, utilise le tutoiement et prioritise les mots simples. Utilise le format Markdown pour les titres, liens, gras, italique, etc:"""
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

    response = client.chat.completions.create(
        model=config['OPENAI_MODEL'],
        messages=[
            {"role": "system", "content": system_messages[output_language]},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        max_completion_tokens=5000 # Increased max tokens to allow longer responses
    )

    print(f"✓ Article generated from transcript for '{title}' from '{source_language}' to '{output_language}'")

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
    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])

    prompts = {
        'en': f'''Generate exactly 5 unique and relevant tags in English for this article. Return them as a JSON object with a "tags" key containing the array.

Title: "{title}"
Content: {article_content[:1000]}

The response should look exactly like this:
{{"tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}}''',

        'fr': f'''Génère exactement 5 tags uniques et pertinents en français pour cet article. Renvoie-les sous forme d'objet JSON avec une clé "tags" contenant le tableau.

Titre: "{title}"
Contenu : {article_content[:1000]}

La réponse doit ressembler exactement à ceci :
{{"tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}}'''
    }

    system_messages = {
        'en': 'You are a tag generator that only outputs valid JSON objects with a "tags" array containing exactly 5 tags.',
        'fr': 'Tu es un générateur de tags qui ne produit que des objets JSON valides avec un tableau "tags" contenant exactement 5 tags.'
    }

    # Default tags for each language
    default_tags = {
        'en': ["self help", "psychology", "self improvement", "personal development", "personal growth"],
        'fr': ["développement personnel", "psychologie", "croissance personnelle", "motivation", "bien-être"]
    }

    # Get the appropriate prompt and system message based on the output language
    prompt = prompts.get(output_language, prompts['en'])
    system_message = system_messages.get(output_language, system_messages['en'])

    try:
        response = client.chat.completions.create(
            model=config['OPENAI_MODEL'],
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_completion_tokens=100,
            response_format={"type": "json_object"}
        )

        # Get the response content
        content = response.choices[0].message.content.strip()

        try:
            # Parse the JSON response
            parsed_response = json.loads(content)

            # Extract tags array from the response
            if isinstance(parsed_response, dict) and "tags" in parsed_response:
                tags = parsed_response["tags"]

                # Validate that we got a list of strings
                if isinstance(tags, list) and len(tags) > 0 and all(isinstance(tag, str) for tag in tags):
                    print(f"✓ Relevant tags (topics) generated: {tags[:5]}")
                    return tags[:5]  # Ensure we return exactly 5 tags

            print(f"Invalid tags format. Using default tags instead. Error: {parsed_response}")
            return default_tags[output_language]

        except json.JSONDecodeError as je:
            print(f"JSON parsing error: {je}. Response content: {content}")
            return default_tags[output_language]

    except Exception as e:
        print(f"Error generating tags: {e}")
        return default_tags[output_language]

def generate_article_title(article_content: str, output_language: str = 'en') -> str:
    """
    Generate an engaging title for Medium.com article in either English or French.

    Args:
        article_content: The content of the article
        output_language: Target language ('en' or 'fr')

    Returns:
        str: Generated title in the specified language
    """
    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])

    prompts = {
        'en': f"""You are an expert content writer. Based on the content provided below, generate an engaging title for a Medium.com article.

    Content: {article_content[:1000]}  # Limit the content sent to the model
    
    Ensure the title grabs attention and would entice readers on Medium.com to click and read the story. The title should be creative and concise, ideally under 60 characters.
    Whenever possible, use one of the following formats: "How [Action|Benefit] WITHOUT [Pain Point]?", "How to [Action|Benefit] in [Limited Time]?", "The New Way to [Action] With No [Friction Point]", "Use/Adopt [Skill|Action] or [Consequence]".

    Don't use irrelevant adjective like Unlock, Effortless, Evolving, Embrace, Enrich, Unleash, Unmask, Unveil, Streamline, Fast-paced, Game-changer, ... and prioritize simple words.""",

        'fr': f"""Tu es un expert en rédaction de contenu. À partir du contenu fourni ci-dessous, génère un titre accrocheur pour un article Medium.com.

    Contenu: {article_content[:1000]}  # Limite le contenu envoyé au modèle
    
    Assure-toi que le titre attire l'attention et donne envie aux lecteurs de Medium.com de cliquer et de lire l'histoire. Le titre doit être créatif et concis, idéalement moins de 60 caractères.
    Dans la mesure du possible, utilise l'un des formats suivants : "Comment [Action|Bénéfice] SANS [Point de Douleur] ?", "Comment [Action|Bénéfice] en [Temps Limité] ?", "La Nouvelle Façon de [Action] SANS [Point de Friction]", "Faites [Compétence/Action] ou [Conséquence]".
    Utilise le tutoiement et utilise des mots simples. N'utilise aucun adjectif non pertinent ou compliqué comme Débloquer, Dévoiler, Démasquer, Révéler, Rationaliser, Révolutionnaire."""
    }

    system_messages = {
        'en': "You are an expert content writer and title generator.",
        'fr': "Tu es un expert en rédaction de contenu et en génération de titres."
    }

    # Use the appropriate prompt and system message based on the output language
    prompt = prompts.get(output_language, prompts['en'])  # Default to English if language not found
    system_message = system_messages.get(output_language, system_messages['en'])

    response = client.chat.completions.create(
        model=config['OPENAI_MODEL'],
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_completion_tokens=100
    )

    title = response.choices[0].message.content.strip('"')
    print(f"✓ Generated article title: {title}")
    return title

def fetch_images_from_unsplash(query: str, article_title: str, output_language: str = 'en', per_page: int = 2) -> Optional[List[UnsplashImage]]:
    """
    Fetch images from Unsplash, optionally prioritizing images from the preferred photographer if configured.
    One image for header, and 1-2 for content.
    Supports both English and French captions with article title.

    Args:
        query: Search query string for images (can be multiple terms separated by spaces)
        article_title: The title of the article to use in captions
        output_language: Target language ('en' or 'fr')
        per_page: Number of images to fetch (default: 2)
    Returns:
        Optional[List[UnsplashImage]]: List of UnsplashImage objects with URLs, alt text, and attribution captions in Markdown
    """
    unsplash_access_key = config['UNSPLASH_ACCESS_KEY']
    preferred_photographer = config.get('UNSPLASH_PREFERRED_PHOTOGRAPHER')
    results = []
    
    try:
        print(f"Fetching images from Unsplash for query: '{query}")

        # If preferred photographer is configured, try to get their images first
        if preferred_photographer:
            user_url = (
                f"https://api.unsplash.com/users/{preferred_photographer}/photos"
                f"?query={query}"
                f"?client_id={unsplash_access_key}"
                f"&per_page={per_page}"
                f"&orientation=landscape"
            )
            
            user_response = requests.get(user_url)
            user_response.raise_for_status()
            results = user_response.json()
            
            print(f"✓ Fetched {len(results)} images from preferred photographer (@{preferred_photographer})")
        
        # If we need more images (either no preferred photographer or not enough images from them)
        if len(results) < per_page:
            remaining_images = per_page - len(results)
            search_url = (
                f"https://api.unsplash.com/search/photos"
                f"?query={query}"
                f"&client_id={unsplash_access_key}"
                f"&per_page={remaining_images}"
                f"&orientation=landscape"
            )
            
            search_response = requests.get(search_url)
            search_response.raise_for_status()
            search_results = search_response.json()

            search_photos = search_results.get('results', [])
            results.extend(search_photos)
            print(f"✓ Fetched {len(search_photos)} additional images from general search")

        results = results[:per_page]  # Ensure we don't exceed the requested number of images

        captions = {
            'en': lambda name, photo_url, profile_url: f"{article_title} - Photo by [{name}]({profile_url}) on [Unsplash]({photo_url})",
            'fr': lambda name, photo_url, profile_url: f"{article_title} - Photo de [{name}]({profile_url}) sur [Unsplash]({photo_url})"
        }

        caption_formatter = captions.get(output_language, captions['en'])

        return [
            UnsplashImage(
                url=result.get('urls', {}).get('regular'),
                alt=result.get('description') or (
                    f"Photo par {result.get('user', {}).get('name')}" if output_language == 'fr' 
                    else f"Photo by {result.get('user', {}).get('name')}"
                ),
                caption=caption_formatter(
                    result.get('user', {}).get('name'),
                    result.get('links', {}).get('html'),
                    result.get('user', {}).get('links', {}).get('html')
                )
            )
            for result in results
            if result.get('urls') and result.get('user')
        ]
    except Exception as e:
        print(f"Failed to fetch images from Unsplash: {e}")
        return None

def embed_images_in_content(article_content: str, images: List[UnsplashImage], article_title: str) -> str:
    """
    Embed 1-3 images in the article content using Medium-compatible Markdown format.
    First image is always placed as the header image, additional images (if any)
    are distributed through the content at 1/3 and 2/3 positions.

    Args:
        article_content: The main article content
        images: List of UnsplashImage objects (1-3 images)
        article_title: Title of the article for image title attribute

    Returns:
        str: Article content with embedded images and their captions
    """
    if not images:
        return article_content

    def create_image_block(image: UnsplashImage) -> str:
        return f"""![{image.alt}]({image.url} "{article_title}")
*{image.caption}*\n\n"""

    # Split content into paragraphs
    paragraphs = article_content.split('\n\n')

    # Calculate insertion points for additional images
    one_third = len(paragraphs) // 3
    two_thirds = (len(paragraphs) * 2) // 3

    # Start with header image
    result = [create_image_block(images[0])]

    # Add content with additional images if available
    for i, paragraph in enumerate(paragraphs):
        result.append(paragraph)

        # Add second image at 1/3 point if available
        if i == one_third and len(images) >= 2:
            result.append(create_image_block(images[1]))

        # Add third image at 2/3 point if available
        if i == two_thirds and len(images) >= 3:
            result.append(create_image_block(images[2]))

    return '\n\n'.join(result)

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
    # Create the base_dir directory if it doesn't exist
    os.makedirs(base_dir, exist_ok=True)

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

    metadata_header: str = f"""---
original_title: {original_title}
optimized_title: {title}
medium_url: {medium_url}
date: {datetime.now().isoformat()}
tags: {formatted_tags}
---

"""

    try:
        with open(file_name, "w", encoding="utf-8") as file:
            # Write yaml-like metadata
            file.write(metadata_header)
            # Write article content
            file.write(article)
    except (OSError, UnicodeEncodeError) as e:
        print(f"Error saving article: {e}")
        raise

    print(f"✓ Article saved locally at: {file_name}")
    return file_name

def post_to_medium(title: str, content: str, tags: List[str], output_language: str) -> Optional[str]:
    """
    Post article to Medium with support for publication posting.
    """
    config = load_config()
    en_publication_id = config.get('MEDIUM_EN_PUBLICATION_ID')
    fr_publication_id = config.get('MEDIUM_FR_PUBLICATION_ID')
    post_to_publication = config.get('POST_TO_PUBLICATION', False)
    token = config['MEDIUM_ACCESS_TOKEN']
    publish_status = config['PUBLISH_STATUS']

    # Prepare article in Markdown format
    full_content = f"# {title}\n\n{content}"

    article = {
        "title": title,
        "contentFormat": "markdown",
        "content": full_content,
        "tags": tags[:5],  # Medium allows up to 5 tags
        "publishStatus": publish_status
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

        print(f"✓ Article posted to Medium")
        return response.json()["data"]["url"]

    except Exception as e:
        print(f"Failed to post article: {e}")
        print(f"Response: {response.text if 'response' in locals() else 'No response'}")
        return None

def check_article_exists(title: str, base_dir: str = 'articles') -> Optional[str]:
    """
    Check if an article already exists locally based on the title.

    Args:
        title (str): The title of the article
        base_dir (str, optional): The base directory to search for articles. Defaults to 'articles'.

    Returns:
        Optional[str]: The path of the existing article file or None if it doesn't exist.
    """
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    file_name = os.path.join(base_dir, f"{safe_title}.md")
    return file_name if os.path.exists(file_name) else None


def main():
    youtube = get_authenticated_service()
    channel_id = config['YOUTUBE_CHANNEL_ID']
    videos = get_channel_videos(youtube, channel_id)

    print(f"Found {len(videos)} videos in the channel")

    source_language = config.get('SOURCE_LANGUAGE', 'fr')
    output_language = config.get('OUTPUT_LANGUAGE', 'en')

    for index, video in enumerate(videos, 1): # Start from 1
        # Skip if article already exists
        if check_article_exists(
            video.title,
            base_dir=f"articles/{output_language}" if output_language != 'en' else 'articles'
        ):
            print(f"Already exists locally. Skipping '{video.title}'")
            continue

        print(f"Waiting for {RATE_LIMIT_PERIOD_SECONDS} seconds before processing the next video...")
        print_progress_separator(index, len(videos), video.title)

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
            optimized_title = generate_article_title(article, output_language=output_language)

            # Retrieve images. Number of images depends if the article is long or short
            images_per_article = 3 if len(article) > LONG_ARTICLE_THRESHOLD else 2
            # Create search query from first 3 tags
            search_query = ' '.join(tags[:3])
            images = fetch_images_from_unsplash(
                query=search_query,
                article_title=optimized_title,
                output_language=output_language,
                per_page=images_per_article
            )
            if images:
                article = embed_images_in_content(article, images, optimized_title)

            # Set default medium_url
            medium_url = "not_published"

            # Try to post to Medium, but continue saving article locally if fails
            try:
                medium_result = post_to_medium(optimized_title, article, tags, output_language)
                if medium_result:
                    medium_url = medium_result
                    print(f"✓ Article available at: {medium_url}")
            except Exception as e:
                print(f"✗ Failed to post to Medium: {e}")

            save_article_locally(
                "not_published_" + video.title if medium_url == "not_published" else video.title,
                optimized_title,
                tags,
                article,
                medium_url,
                base_dir=f"articles/{output_language}" if output_language != 'en' else 'articles'
            )

        except Exception as e:
            print(f"Error processing video {video.title}: {e}")

if __name__ == "__main__":
    main()
