import os
import json
import re
import time
import random
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

# Markdown to HTML conversion
import markdown as md_lib

# Multi-platform publishing layer (Medium, Dev.to, Hashnode, ...)
from publishers import build_publishers, publish_to_all, select_primary_url

RATE_LIMIT_PERIOD_SECONDS = 300 # 5 minute
MAX_CALLS_IN_PERIOD = 1
LONG_ARTICLE_THRESHOLD = 2499
VERY_LONG_ARTICLE_THRESHOLD = 5800

# Video duration thresholds (in seconds)
SHORT_VIDEO_DURATION = 600  # 10 minutes
MEDIUM_VIDEO_DURATION = 1800  # 30 minutes
LONG_VIDEO_DURATION = 2400  # 40 minutes - optimal threshold for extended articles
VERY_LONG_VIDEO_DURATION = 3600  # 60 minutes

# Transcript character limits based on video duration
# Balance between context capture and API costs
SHORT_VIDEO_TRANSCRIPT_LIMIT = 40800  # ~8k-10k words
MEDIUM_VIDEO_TRANSCRIPT_LIMIT = 102000  # ~20k-25k words (2.5x base)
LONG_VIDEO_TRANSCRIPT_LIMIT = 204000  # ~40k-50k words (5x base) - for 40min videos
VERY_LONG_VIDEO_TRANSCRIPT_LIMIT = 306000  # ~60k-75k words (7.5x base) - for 60min+ videos


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
    duration_seconds: int = 0

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
        # Use the new API (v1.2.0+): create instance and use fetch() method directly
        ytt_api = youtube_transcript_api.YouTubeTranscriptApi()

        try:
            # First try to get transcript in the requested language directly
            fetched_transcript = ytt_api.fetch(video_id, languages=[language])
            print(f"✓ Found direct transcript in {language}")
            # Convert to raw data format (list of dicts with 'text' key)
            transcript_data = fetched_transcript.to_raw_data()
            return " ".join([entry["text"] for entry in transcript_data])
        except Exception:
            try:
                # If not found, try to get French auto-generated transcript first
                fetched_transcript = ytt_api.fetch(video_id, languages=['fr'])
                if language != 'fr':
                    # Use the list/translate approach for translation
                    transcript_list = ytt_api.list(video_id)
                    transcript = transcript_list.find_generated_transcript([
                                                                           'fr'])
                    transcript = transcript.translate(language)
                    transcript_data = transcript.fetch().to_raw_data()
                    print(
                        f"✓ Using translated transcript from French to {language}")
                else:
                    transcript_data = fetched_transcript.to_raw_data()
                    print("✓ Using French auto-generated transcript")
                return " ".join([entry["text"] for entry in transcript_data])
            except Exception as e:
                print(f"✗ No transcript available in any format: {e}")
                return None

    except Exception as e:
        print(
            f"✗ Error fetching transcript for video {video_id} in {language}: {e}")
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
                response = get_videos_page(
                    youtube, uploads_playlist_id, next_page_token)

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
                                published_at=item["snippet"]["publishedAt"],
                                duration_seconds=duration_seconds
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

def generate_article_from_transcript(transcript: str, title: str, source_language: str = 'fr', output_language: str = 'en', video_duration: int = 0, niche: str = 'self-help') -> str:
    """
    Generate article from transcript with dynamic handling based on video duration.
    
    Args:
        transcript: Video transcript text
        title: Video title
        source_language: Source language code
        output_language: Output language code
        video_duration: Video duration in seconds
        niche: Content niche ('self-help' or 'tech')
    
    Returns:
        Generated article content
    """
    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])
    
    # Determine transcript limit and max tokens based on video duration
    # For 40+ minute videos, capture significantly more context for quality articles
    if video_duration > VERY_LONG_VIDEO_DURATION:
        transcript_limit = VERY_LONG_VIDEO_TRANSCRIPT_LIMIT
        max_tokens = 16000
        print(f"✓ Processing very long video ({video_duration//60} minutes) with maximum context capture")
    elif video_duration > LONG_VIDEO_DURATION:
        # Sweet spot for 40-60 minute videos
        transcript_limit = LONG_VIDEO_TRANSCRIPT_LIMIT
        max_tokens = 12000
        print(f"✓ Processing long video ({video_duration//60} minutes) with extended context for in-depth article")
    elif video_duration > MEDIUM_VIDEO_DURATION:
        transcript_limit = MEDIUM_VIDEO_TRANSCRIPT_LIMIT
        max_tokens = 8000
        print(f"✓ Processing medium-long video ({video_duration//60} minutes) with enhanced context")
    elif video_duration > SHORT_VIDEO_DURATION:
        transcript_limit = MEDIUM_VIDEO_TRANSCRIPT_LIMIT
        max_tokens = 7000
        print(f"✓ Processing medium video ({video_duration//60} minutes)")
    else:
        transcript_limit = SHORT_VIDEO_TRANSCRIPT_LIMIT
        max_tokens = 5000
        print(f"✓ Processing short video ({video_duration//60} minutes)")

    # For very long transcripts, use intelligent sampling to capture key content
    transcript_to_use = transcript[:transcript_limit]
    if len(transcript) > transcript_limit and video_duration > LONG_VIDEO_DURATION:
        # Capture beginning (40%), middle (30%), and end (30%) for comprehensive coverage
        beginning_size = int(transcript_limit * 0.4)
        middle_size = int(transcript_limit * 0.3)
        end_size = transcript_limit - beginning_size - middle_size

        middle_start = (len(transcript) - middle_size) // 2
        end_start = len(transcript) - end_size

        transcript_to_use = (
            transcript[:beginning_size] +
            "\n\n[... middle section of video ...]\n\n" +
            transcript[middle_start:middle_start + middle_size] +
            "\n\n[... continuing to conclusion ...]\n\n" +
            transcript[end_start:]
        )
        print(f"✓ Using intelligent sampling: capturing beginning, key middle section, and conclusion")

    # Define instructions and prompts for both English and French languages
    instructions = {
        'en': {
            'fr': "Translate the following French YouTube video transcript into English. Remove all promotional content, superfluous, 'Subscribe to my channel', introductions, conclusions.",
            'en': "Translate the following YouTube video transcript and remove all promotional content, superfluous, 'Subscribe to my channel', introductions, conclusions.",
            'other': lambda lang: f"Translate the following {lang} YouTube video transcript into English and remove all promotional content, superfluous, 'Subscribe to my channel', introductions, conclusions."
        },
        'fr': {
            'fr': "Reformule la transcription vidéo YouTube suivante en français,",
            'en': "Traduis la transcription vidéo YouTube suivante en français et supprime tout contenu superflus, promotionnel, les appels à s'abonner, les introductions et les conclusions,",
            'other': lambda lang: f"Traduis la transcription vidéo YouTube suivante du {lang} vers le français, supprime tout contenu superflu, promotionnel, les appels à s'abonner, les introductions et les conclusions,",
        }
    }

    # Define niche-specific prompts
    if niche == 'tech':
        # Tech CTAs - randomly select 2-3 platforms to avoid overwhelming readers
        tech_ctas = [
            "Get inspired by [open-source projects I've built](https://github.com/pH-7) over the years",
            "Follow my [AI & tech journey on Substack](https://substack.com/@pierrehenry)",
            "Check out [my book on PRO coding practices](https://github.com/pH-7/GoodJsCode)",
            "[Learn more about me on Dev.to](https://dev.to/pierre)",
            "[Support my work with a coffee](https://ko-fi.com/phenry) if this helped you",
            "[Subscribe to my YouTube channel](https://www.youtube.com/@pH7Programming) for weekly programming videos"
        ]
        
        # Randomly select 2-3 CTAs
        num_ctas = random.randint(2, 3)
        selected_ctas = random.sample(tech_ctas, num_ctas)
        tech_cta_section = '\n'.join(selected_ctas)
        
        prompts = {
            'en': f"""{{instruction}} Remove all filler sounds and verbal tics.
    Rewrite this as a well-structured technical article for "NextGen Dev: AI & Software Development", skipping video intro/outro and promotional content.
    
    WRITING QUALITY: Craft genuinely engaging prose. Vary sentence length and structure. Use vivid, precise language. Build momentum through smooth transitions. Hook readers from the first line and maintain their interest throughout.

    CRITICAL: Preserve the speaker's EXACT voice, tone, and personality. Match their speaking style precisely:
    - Keep their casual/formal tone, first-person perspective, directness, and enthusiasm
    - Maintain their teaching style, analogies, and personal experiences
    - Avoid generic corporate tech blog voice - write as the speaker would write
    
    For longer content, develop technical concepts with code examples and practical insights. Create natural narrative flow.
    End with "Key Takeaways" bullet points. Include 1-2 relevant technical quotes if appropriate.
    Quotes must use this exact Markdown shape:
    > *Quote text without surrounding double quotes*
    >
    > — Author
    
    After a Markdown separator, add this CTA section in the same voice as the article:
    {tech_cta_section}

    Kicker: Very short bold text above the title (use **bold**, never a heading).
    Title: {title}
    Subtitle: Optional concise technical subtitle as ### H3 heading.

    Transcript: {transcript_to_use}

    Format as Medium.com article. Use ## for section headings and ### for subsection headings only (Medium ignores #### and below).
    DO NOT use em dashes anywhere in the article body, headings, captions, bullets, or transitions. The only allowed em dash is the attribution marker in quote blocks: > — Author. DO NOT use emojis. Avoid unnecessary buzzwords and corporate jargon unless the speaker uses them. Highlight a few important sentences if any.
    Use Markdown for headings, code blocks, links, bold, italic:"""
        }
    else:  # self-help niche
        prompts = {
            'en': f"""{{instruction}} Remove all filler sounds like "euh...", "bah", "ben", "hein" and similar verbal tics.

    WRITING QUALITY: Craft genuinely engaging prose that captivates readers. Vary sentence rhythm - mix short punchy sentences with longer flowing ones. Use vivid, concrete language over abstract concepts. Build momentum through smooth transitions between ideas. Hook readers from the opening line and reward them throughout.

    While ensuring em dashes aren't used, rewrite it as a well-structured, comprehensive article in English, skipping the video introduction (e.g. Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), the ending section (e.g. au revoir, code de promotion, code de réduction, je te retouve dans mes formations, les liens en dessous de la vidéo, à bientôt, ciao, n'oublie pas de t'abonner, ...), CTA related to PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com and workshops.
    Ensure it reads well and doesn't sound like a transcript, though the article must keep the exact same personal, positive, and motivational voice tone and unique written style markers as the transcript, and emphasise or highlight personal ideas that could fascinate the readers. Pay special attention to French idioms and expressions, translating them to their natural English equivalents.
    For longer content, develop each key concept thoroughly with examples, actionable steps, and deeper insights. Create a cohesive narrative that flows naturally from one idea to the next.
    End the article with one or two properly formatted subheading-style questions (e.g. ### TL;DR, ### Key Takeaways, ### Key Lessons, ### Actions List, ### Ask Yourself, ### What About You). Follow each heading with short bullet or numbered points. Precede the heading with a Markdown separator (---).
    If relevant to article's theme, include 1 to 3 impactful quotes in different places throughout the article that deeply resonate with the article's message. Use this exact format:

> *Quote text here. Only this line should be italicized.*
>
> — Author

    Only the quote text must be italicized. Do not italicize the author line. The em dash (—) is only allowed in the attribution.
    Lastly, in the exact same personal voice tone as the transcript, lead readers to read my complementary book available at https://book.ph7.me (use anchor text such as "my self-help guide" and emphasize/bold it). Suggest my podcast https://podcasts.ph7.me co-hosted with El, and/or invite them subscribe to my private mailing list to receive exclusive software engineering insights I share at https://masterclass.ph7.me (always use anchor text for links), preceded by another Markdown separator.

    Kicker: Right before Title, very short bold text (use **bold**, never a heading).
    Title: {title}
    Subtitle: Right after Title, optional concise appealing / clickbait as ### H3 heading.

    Transcript: {transcript_to_use}

    Structured as a Medium.com article in English while keeping the identical same voice tone as in the original transcript.
    Use ## for section headings and ### for subsection headings only (Medium ignores #### and below).
    Use simple words. DO NOT use em dashes anywhere in the article body, headings, captions, bullets, or transitions. The only allowed em dash is the attribution marker in quote blocks: > — Author. DO NOT use any emojis, and DO NOT use any unnecessary or complicated adjective such as: Unlock, Effortless, Explore, Insights, Today's Digital World, In today's world, Dive into, Refine, Evolving, Embrace, Embracing, Embark, Enrich, Envision, Unleash, Unmask, Unveil, Streamline, Fast-paced, Delve, Digital Age, Game-changer, Indulge, Merely, Endure.
    Use Markdown format for headings, links, bold, italic, etc:""",

        }
        if output_language == 'fr':  # Only self-help niche has French output
            prompts['fr'] = f"""{{instruction}} en supprimant les sons de remplissage comme "euh...", "bah", "ben", "hein" et autres tics verbaux similaires.

    QUALITÉ RÉDACTIONNELLE: Rédige une prose captivante et soignée. Varie le rythme des phrases - alterne entre phrases courtes percutantes et phrases plus longues et fluides. Utilise un langage vivant et concret. Crée des transitions fluides entre les idées. Accroche le lecteur dès la première ligne et maintiens son intérêt tout au long de l'article.

    Réécris-le sous forme d'article bien structuré en français, en omettant l'introduction vidéo (ex: Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), la conclusion (ex: au revoir,  code de promotion, code de réduction, je te retouve dans mes formations, les liens en dessous de la vidéo, à bientôt, ciao, N'oublie pas de t'abonner, ...), et exclus toute promotion liée à PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com et aux ateliers.
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

    Pour les contenus plus longs, développe chaque concept clé en profondeur avec des exemples, des étapes actionnables et des insights plus approfondis. Crée un récit cohérent qui s'enchaîne naturellement d'une idée à l'autre.
    Si cela est pertinent avec l'article, inclue 1 à 3 citations dispersées dans l'article et percutantes qui résonnent profondément avec le message de l'article. Utilise ce format exact :

> *Texte de citation ici. Seule cette ligne doit être en italique.*
>
> — Auteur

    Seul le texte de la citation doit être en italique. Ne mets pas la ligne d'auteur en italique. Le tiret cadratin (—) n'est autorisé que dans l'attribution.
    Termine l'article par un ou deux sous-titre (par exemple : ## Points Clés, ## Récap, ## Actions à prendre, ## Demandez-vous). Fais suivre chaque sous-titre par des points ou d'une liste numérotée. Précède le titre d'un séparateur Markdown (---).
    Enfin, suggérer le lecteur de s'inscrire à ma liste de contacts sur https://contacts.ph7.me (utilise un texte d'ancrage), précédé d'un séparateur Markdown.

    Kicker: Juste avant le Titre, très courte phrase d'accroche en gras (utilise **gras**, jamais un titre/heading).
    Titre: {title}
    Sous-titre: Juste après le Titre, sous-titre optionnel en ### H3, qui donne une promesse concise qui aguiche/intrigue davantage.

    Transcription: {transcript_to_use}

    Structure le texte en tant qu'article Medium.com français tout en gardant le même ton de voix que dans la transcription, utilise le tutoiement et prioritise les mots simples.
    Utilise ## pour les titres de section et ### pour les sous-titres de section uniquement (Medium ignore #### et en dessous).
    N'utilise jamais de tiret cadratin dans le corps de l'article, les titres, les légendes, les listes ou les transitions. Le seul tiret cadratin autorisé est le marqueur d'attribution des citations : > — Auteur. N'utilise jamais d'emojis. Utilise le format Markdown pour les titres, liens, gras, italique, etc:"""

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
        'en': "You are a translator and content writer expert",
        'fr': "Tu es un expert en rédaction de contenu en ligne"
    }

    response = client.chat.completions.create(
        model=config['OPENAI_MODEL'],
        messages=[
            {"role": "system", "content": system_messages[output_language]},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        max_completion_tokens=max_tokens
    )

    article_content = response.choices[0].message.content

    print(
        f"✓ Article generated from transcript for '{title}' from '{source_language}' to '{output_language}'")
    print(f"✓ Article length: {len(article_content)} characters, used {len(transcript_to_use)} chars of transcript (from {len(transcript)} total)")

    return article_content
def generate_tags(article_content: str, title: str, output_language: str = 'en', niche: str = 'self-help') -> List[str]:
    """
    Generate tags for an article in either English or French.

    Args:
        article_content: The content of the article
        title: The title of the article
        output_language: Target language ('en' or 'fr')
        niche: Content niche ('self-help' or 'tech')

    Returns:
        List[str]: List of exactly 5 tags in the specified language
    """
    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])

    prompts = {
        'en': f'''Generate exactly 5 unique and relevant tags in English for this article. Return them as a JSON object with a "tags" key containing the array.

Title: "{title}"
Content: {article_content[:300]}

The response should look exactly like this:
{{"tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}}''',

        'fr': f'''Génère exactement 5 tags uniques et pertinents en français pour cet article. Renvoie-les sous forme d'objet JSON avec une clé "tags" contenant le tableau.

Titre: "{title}"
Contenu : {article_content[:300]}

La réponse doit ressembler exactement à ceci :
{{"tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}}'''
    }

    system_messages = {
        'en': 'You are a tag generator that only outputs valid JSON objects with a "tags" array containing exactly 5 tags',
        'fr': 'Tu es un générateur de tags qui ne produit que des objets JSON valides avec un tableau "tags" contenant exactement 5 tags'
    }

    # Default tags for each language and niche
    default_tags = {
        'self-help': {
            'en': ["Self Help", "Psychology", "Self Improvement", "Personal Development", "Personal Growth"],
            'fr': ["Développement Personnel", "Psychologie", "Croissance Personnelle", "Motivation", "Bien-Être"]
        },
        'tech': {
            'en': ["Programming", "Software Development", "Coding", "Technology", "Developer Tools"],
            'fr': ["Programmation", "Développement Logiciel", "Codage", "Technologie", "Outils Développeur"]
        }
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

            print(
                f"✗ Invalid tags format. Using default tags instead. Error: {parsed_response}")
            return default_tags.get(niche, default_tags['self-help'])[output_language]

        except json.JSONDecodeError as je:
            print(f"JSON parsing error: {je}. Response content: {content}")
            return default_tags.get(niche, default_tags['self-help'])[output_language]

    except Exception as e:
        print(f"✗ Error generating tags: {e}")
        return default_tags.get(niche, default_tags['self-help'])[output_language]

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
        'en': f"""Based on the content below, generate a strong title + subtitle for a Medium article.

Content: {article_content[:620]}

GOAL: Create titles that are honest, specific, and arouse genuine curiosity by showing a desirable outcome while acknowledging real friction or pain.

PREFERRED TITLE STRUCTURES (use when they fit naturally):
1. "How to [achieve desired outcome] without [pain/friction/struggle]"   ← Strongest default
2. "The new way to [achieve desired outcome]"
3. "How to [achieve desired outcome] in [X hours/days/weeks]"

Other strong patterns when relevant:
- "Stop [painful thing]. Do [better approach] instead"
- "[Action] without [common expensive/hard thing]"

RULES:
- First identify the core painful problem or frustrating situation the reader is facing.
- The title must stay grounded in the actual content. No exaggeration or false promises.
- Create titles that make the reader think: “This sounds useful… I wonder how they do that without the usual pain.”
- Combine honesty with a light sense of possibility and curiosity.
- Keep the main title under 70 characters when possible.
- Avoid hype words: Unlock, Effortless, Game-changer, Revolutionary, etc. Use simple, concrete language.

""",

        'fr': f"""À partir du contenu ci-dessous, génère un titre + sous-titre forts pour un article Medium.

Contenu: {article_content[:620]}

OBJECTIF : Créer des titres honnêtes, précis, qui éveillent une curiosité sincère en montrant un résultat désirable tout en reconnaissant les difficultés réelles.

STRUCTURES DE TITRES PRÉFÉRÉES (à utiliser quand elles collent naturellement) :
1. "Comment [obtenir le résultat souhaité] sans [galère / difficulté / friction]"   ← Structure la plus forte
2. "La nouvelle façon d'[obtenir le résultat souhaité]"
3. "Comment [obtenir le résultat souhaité] en [X heures/jours/semaines]"

Autres structures puissantes quand pertinentes :
- "Arrête [chose pénible]. Fais [meilleure approche] à la place"
- "[Action] sans [chose chère ou difficile]"

RÈGLES :
- Identifie d'abord le problème douloureux ou la situation frustrante principale que vit le lecteur.
- Le titre doit rester ancré dans le contenu réel. Pas d'exagération ni de fausse promesse.
- Crée des titres qui donnent envie au lecteur de se dire : « Ça a l'air utile… Je me demande comment ils font ça sans la galère habituelle. »
- Combine honnêteté et une légère sensation de possibilité et de curiosité.
- Garde le titre principal idéalement sous 70 caractères.
- Évite les mots hype : Débloquer, Sans effort, Révolutionnaire, etc. Utilise un langage simple et concret.

"""
    }

    system_messages = {
        'en': "You are a SEO copywriter expert for writing article headings",
        'fr': "Tu es un expert SEO en rédaction de titres pour articles de blog"
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

    title = remove_disallowed_em_dashes(response.choices[0].message.content.strip('"'))
    print(f"✓ Generated article title: {title}")
    return title

def generate_unsplash_search_queries(article_title: str, article_snippet: str, tags: List[str], num_images: int, output_language: str = 'en') -> List[str]:
    """
    Use GPT to generate specific, visually evocative Unsplash search queries, one per
    image slot. Queries are always in English (Unsplash indexes in English), and each one
    targets a distinct visual concept so that the images feel varied and meaningful.

    Args:
        article_title: Optimised article title
        article_snippet: First ~500 chars of the article body
        tags: Article topic tags (used as thematic hints)
        num_images: How many distinct queries to generate
        output_language: Article output language (used for context only)

    Returns:
        List[str]: List of Unsplash search query strings (always in English)
    """
    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])

    prompt = f"""You are helping curate the best stock photos for a blog article on Medium.

Article title: "{article_title}"
Article tags: {', '.join(tags)}
Article excerpt: {article_snippet[:450]}

Generate exactly {num_images} distinct Unsplash search queries to find photos that visually represent different aspects or moods of this article.

Requirements for each query:
- 2 to 4 words, concrete and visually specific (describe a real scene or object)
- Evocative of the article's emotional tone or a key concept
- Different visual themes from each other, no two should overlap
- Always in English (Unsplash searches best in English)
- Avoid abstract or meta phrases like "personal development", "success mindset", "productivity concept"
- Prefer scenes with people, nature, objects, textures, light, and things a photographer would capture

Good examples: "person writing journal sunrise", "runner morning fog track", "open book warm light", "calm forest path mist"
Bad examples: "personal growth", "motivation success", "self improvement", "mindset concept"

Return ONLY a JSON object: {{"queries": ["query1", "query2", ...]}}"""

    try:
        response = client.chat.completions.create(
            model=config['OPENAI_MODEL'],
            messages=[
                {"role": "system", "content": "You generate precise, vivid visual search queries for stock photo sites. Output only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_completion_tokens=150,
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)
        queries = result.get('queries', [])

        if len(queries) >= num_images:
            print(f"✓ Generated {num_images} targeted Unsplash queries: {queries[:num_images]}")
            return queries[:num_images]

        # Partial result: pad with tag-based fallbacks
        fallback = [article_title] + [' '.join(tags[i:i+2]) for i in range(0, len(tags), 2)]
        queries = (queries + fallback)[:num_images]
        print(f"✓ Generated {len(queries)} queries (padded with fallbacks)")
        return queries

    except Exception as e:
        print(f"✗ Error generating Unsplash search queries: {e}")
        fallback = [article_title] + [' '.join(tags[i:i+2]) for i in range(0, len(tags), 2)]
        return fallback[:num_images]


def generate_unique_image_captions(images: List['UnsplashImage'], article_title: str, article_snippet: str, output_language: str = 'en') -> List[str]:
    """
    Use GPT to generate unique, article-specific caption descriptions for each image.
    Each caption ties the image's visual content to the article's specific message so
    that no two captions look the same, even across similar articles.

    Photographer attribution is handled separately. This function returns only the
    descriptive portion (e.g. "The quiet discipline behind lasting change").

    Args:
        images: List of UnsplashImage objects whose .alt contains the Unsplash description
        article_title: Optimised article title
        article_snippet: First ~400 chars of article body for context
        output_language: 'en' or 'fr'

    Returns:
        List[str]: One unique caption description per image (same order as input)
    """
    if not images:
        return []

    client = openai.OpenAI(api_key=config['OPENAI_API_KEY'])

    image_descs = [img.alt for img in images]
    numbered = '\n'.join(f'{i + 1}. "{d}"' for i, d in enumerate(image_descs))

    lang_instruction = {
        'en': (
            "Write each caption in English. "
            "Each caption must be a short, punchy phrase (4 to 9 words) that connects the image to the article's theme. "
            "Do NOT start with 'A photo of', 'An image of', or any similar phrase."
        ),
        'fr': (
            "Écris chaque légende en français. "
            "Chaque légende doit être une phrase courte et percutante (4 à 9 mots) qui relie l'image au thème de l'article. "
            "Ne commence pas par 'Une photo de', 'Une image de' ou une formule similaire."
        )
    }.get(output_language, "Write each caption in English. Each caption must be a short, punchy phrase (4 to 9 words).")

    prompt = f"""Article title: "{article_title}"
Article excerpt: {article_snippet[:350]}

Unsplash image descriptions:
{numbered}

{lang_instruction}

Additional rules:
- Every caption must be unique. No two can be similar in wording or meaning
- Each caption should feel like it belongs specifically to this article, not a generic photo caption
- Capture the emotional or conceptual connection between image and article message
- Keep it concise: 4 to 9 words maximum
- Do not use em dashes

Return ONLY a JSON object: {{"captions": ["caption1", "caption2", ...]}}"""

    try:
        response = client.chat.completions.create(
            model=config['OPENAI_MODEL'],
            messages=[
                {"role": "system", "content": "You write concise, unique, emotionally resonant image captions for blog articles. Output only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.85,
            max_completion_tokens=200,
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)
        captions = result.get('captions', [])

        if len(captions) >= len(images):
            print(f"✓ Generated {len(images)} unique image captions")
            return [remove_disallowed_em_dashes(caption) for caption in captions[:len(images)]]

        # Pad with article-title-derived fallbacks if GPT returned fewer
        fallback = [f"{article_title} ({i + 1})" for i in range(len(images))]
        captions = (captions + fallback)[:len(images)]
        return [remove_disallowed_em_dashes(caption) for caption in captions]

    except Exception as e:
        print(f"✗ Error generating unique image captions: {e}")
        return [remove_disallowed_em_dashes(img.alt) for img in images]


def fetch_images_from_unsplash(query, article_title: str, output_language: str = 'en', per_page: int = 2) -> Optional[List[UnsplashImage]]:
    """
    Fetch images from Unsplash with recursive fallback for better search results.
    Uses the article title as primary search to get images unique to each article,
    falling back to tags if the title yields no results.
    
    Args:
        query: Can be a string or list of tags
        article_title: The title of the article to use in captions and as primary search
        output_language: Target language ('en' or 'fr')
        per_page: Number of images to fetch (default: 2)
    Returns:
        Optional[List[UnsplashImage]]: List of UnsplashImage objects with URLs, alt text, and attribution captions in Markdown
    """
    # Use article title as primary search (unique per article) for more relevant images
    if isinstance(query, list):
        search_query = article_title if article_title else ' '.join(query[:3])
    else:
        search_query = query
    
    unsplash_access_key = config['UNSPLASH_ACCESS_KEY']
    preferred_photographer = config.get('UNSPLASH_PREFERRED_PHOTOGRAPHER')
    results = []
    # Random page offset (1-3) so articles with similar queries still get different images
    random_page = random.randint(1, 3)
    
    try:
        print(f"✓ Fetching images from Unsplash for query: '{search_query}' (page {random_page})")

        # If preferred photographer is configured, try to get their images first
        if preferred_photographer:
            # Search for images by preferred photographer with the query
            user_search_url = (
                f"https://api.unsplash.com/search/photos"
                f"?query={search_query}"
                f"&username={preferred_photographer}"
                f"&client_id={unsplash_access_key}"
                f"&per_page={per_page}"
                f"&page={random_page}"
                f"&orientation=landscape"
            )
            
            user_response = requests.get(user_search_url)
            if user_response.status_code == 200:
                user_search_results = user_response.json()
                results = user_search_results.get('results', [])
                print(f"✓ Fetched {len(results)} images from preferred photographer (@{preferred_photographer}) for query '{search_query}'")
            else:
                print(f"✗ Failed to fetch from preferred photographer. Status: {user_response.status_code}")
        
        # If we need more images (either no preferred photographer or not enough images from them)
        if len(results) < per_page:
            remaining_images = per_page - len(results)
            search_url = (
                f"https://api.unsplash.com/search/photos"
                f"?query={search_query}"
                f"&client_id={unsplash_access_key}"
                f"&per_page={remaining_images}"
                f"&page={random_page}"
                f"&orientation=landscape"
            )
            
            search_response = requests.get(search_url)
            search_response.raise_for_status()
            search_results = search_response.json()

            search_photos = search_results.get('results', [])
            results.extend(search_photos)
            print(f"✓ Fetched {len(search_photos)} additional images from general search")

        # If no results found, fall back to tags (more generic, broader matches)
        if not results and isinstance(query, list) and search_query == article_title:
            print(f"✗ No images found for title '{search_query}'. Falling back to tags...")
            tag_query = ' '.join(query[:3])
            return fetch_images_from_unsplash(
                query=tag_query,
                article_title=article_title,
                output_language=output_language,
                per_page=per_page
            )
        elif not results and isinstance(query, list) and len(query) > 1:
            print(f"✗ No images found for '{search_query}'. Trying with fewer keywords...")
            return fetch_images_from_unsplash(
                query=query[:-1], # Remove the last tag
                article_title=article_title,
                output_language=output_language,
                per_page=per_page
            )
        elif not results and isinstance(query, str) and ' ' in search_query:
            print(f"✗ No images found for '{search_query}'. Trying with simpler query...")
            # Split the query and try with the first word only
            simplified_query = search_query.split()[0]
            return fetch_images_from_unsplash(
                query=simplified_query,
                article_title=article_title,
                output_language=output_language,
                per_page=per_page
            )

        # Deduplicate images by Unsplash photo ID
        seen_ids = set()
        unique_results = []
        for r in results:
            photo_id = r.get('id')
            if photo_id and photo_id not in seen_ids:
                seen_ids.add(photo_id)
                unique_results.append(r)
        results = unique_results[:per_page]

        if not results:
            print(f"✗ No images found for any search variation")
            return None

        captions = {
            'en': lambda desc, name, profile_url: f"{desc} - Photo by [{name}]({profile_url})",
            'fr': lambda desc, name, profile_url: f"{desc} - Photo de [{name}]({profile_url})"
        }

        caption_formatter = captions.get(output_language, captions['en'])

        processed_images = []
        for result in results:
            if not (result.get('urls') and result.get('user')):
                continue

            # Use Unsplash's alt_description (unique per image, describes actual content)
            # Falls back to description, then to a generic label
            image_desc = (
                result.get('alt_description')
                or result.get('description')
                or article_title
            )
            # Capitalize first letter for cleaner presentation
            image_desc = image_desc[0].upper() + image_desc[1:] if image_desc else article_title

            photographer_name = result.get('user', {}).get('name', 'Unknown')
            profile_url = result.get('user', {}).get('links', {}).get('html', '')

            processed_images.append(
                UnsplashImage(
                    url=result.get('urls', {}).get('regular'),
                    alt=image_desc,
                    caption=caption_formatter(image_desc, photographer_name, profile_url)
                )
            )
        
        print(f"✓ Successfully processed {len(processed_images)} images")
        return processed_images if processed_images else None
        
    except Exception as e:
        print(f"✗ Failed to fetch images from Unsplash: {e}")
        return None


def fetch_images_for_article(queries: List[str], article_title: str, output_language: str = 'en') -> List[UnsplashImage]:
    """
    Fetch one distinct image per query so that every image in an article is visually
    different and topically targeted. Deduplicates across calls by tracking image URLs.

    This replaces the old single-query bulk fetch and gives each article a curated set
    of images rather than a set of loosely related results from one search.

    Args:
        queries: One search query per desired image (produced by generate_unsplash_search_queries)
        article_title: Article title (passed through to fetch_images_from_unsplash for fallback)
        output_language: 'en' or 'fr'

    Returns:
        List[UnsplashImage]: One image per query (fewer if some queries yield no results)
    """
    images: List[UnsplashImage] = []
    seen_urls: set = set()

    for query in queries:
        # Fetch a small batch per query and take the first non-duplicate result
        candidates = fetch_images_from_unsplash(
            query=query,
            article_title=article_title,
            output_language=output_language,
            per_page=3  # Fetch a few extras to have fallback options if URLs collide
        )
        if not candidates:
            print(f"✗ No image found for query '{query}', skipping slot")
            continue

        for candidate in candidates:
            if candidate.url and candidate.url not in seen_urls:
                seen_urls.add(candidate.url)
                images.append(candidate)
                break  # Take only one image per query

    print(f"✓ Collected {len(images)} unique images across {len(queries)} targeted queries")
    return images


def embed_images_in_content(article_content: str, images: List[UnsplashImage], article_title: str) -> str:
    """
    Embed images in the article content using Medium-compatible Markdown format.
    First image is always placed as the header image, additional images
    are evenly distributed through the content.

    Args:
        article_content: The main article content
        images: List of UnsplashImage objects
        article_title: Title of the article

    Returns:
        str: Article content with embedded images and their captions
    """
    if not images:
        return article_content

    def strip_md_links(text: str) -> str:
        """Convert Markdown links [text](url) to just text for use in alt attributes."""
        return re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    def create_image_block(image: UnsplashImage) -> str:
        # Medium's API doesn't reliably render alt text as visible captions,
        # so we add the caption as italic text on the line immediately after the image.
        # The alt attribute gets a plain-text fallback; the visible caption keeps Markdown links.
        alt_caption = strip_md_links(image.caption)
        return f"""![{alt_caption}]({image.url})\n*{image.caption}*\n\n"""

    # Split content into paragraphs
    paragraphs = article_content.split('\n\n')

    # Start with header image
    result = [create_image_block(images[0])]

    # Evenly distribute remaining images through the content
    extra_images = images[1:]
    insertion_points = {
        (len(paragraphs) * (i + 1)) // (len(extra_images) + 1): img
        for i, img in enumerate(extra_images)
    } if extra_images else {}

    for i, paragraph in enumerate(paragraphs):
        result.append(paragraph)

        if i in insertion_points:
            result.append(create_image_block(insertion_points[i]))

    return '\n\n'.join(result)

def embed_youtube_video(article_content: str, video_id: str) -> str:
    """
    Embed YouTube video randomly within the article content using Medium-compatible format.
    Medium automatically converts YouTube URLs into embedded video players.
    The video is placed at a random position (1/4, 1/2, or 3/4) through the article.

    Args:
        article_content: The main article content
        video_id: YouTube video ID

    Returns:
        str: Article content with embedded YouTube video at a random position
    """
    # Split content into paragraphs
    paragraphs = article_content.split('\n\n')
    
    # If article is too short, place at the beginning
    if len(paragraphs) < 4:
        youtube_embed = f"https://www.youtube.com/watch?v={video_id}\n\n---\n\n"
        return youtube_embed + article_content
    
    # Randomly choose insertion point: 1/4, 1/2, or 3/4 of the way through
    position_options = [
        len(paragraphs) // 4,      # 25% through
        len(paragraphs) // 2,      # 50% through
        (len(paragraphs) * 3) // 4  # 75% through
    ]
    insertion_point = random.choice(position_options)
    
    # Create YouTube embed block with separators
    youtube_block = f"---\n\nhttps://www.youtube.com/watch?v={video_id}\n\n---"
    
    # Insert the video at the chosen position
    result = paragraphs[:insertion_point] + [youtube_block] + paragraphs[insertion_point:]
    
    return '\n\n'.join(result)

def separate_consecutive_quotes(content: str) -> str:
    """
    Ensure no two Markdown blockquote blocks appear back-to-back in the article.

    When consecutive blockquotes are found, the second (and any further adjacent ones)
    are deferred and re-inserted after the next non-quote paragraph. This guarantees
    quotes are always separated by at least one paragraph of regular text, which makes
    the article read more naturally and prevents the visual clutter of stacked quote blocks.

    Args:
        content: Markdown article content (post-blockquote normalisation)

    Returns:
        str: Content with blockquotes properly spread throughout the text
    """
    # Split on double (or more) newlines to get individual paragraph units
    paragraphs = re.split(r'\n{2,}', content)

    def is_blockquote_block(para: str) -> bool:
        """Return True if every non-empty line in the paragraph starts with '>'."""
        stripped = para.strip()
        if not stripped:
            return False
        return all(line.startswith('>') or line.strip() == '' for line in stripped.split('\n'))

    result: List[str] = []
    deferred: List[str] = []

    for para in paragraphs:
        if is_blockquote_block(para):
            # Look at the most recent non-empty paragraph already committed to result
            prev_non_empty = next((p for p in reversed(result) if p.strip()), None)
            if prev_non_empty is not None and is_blockquote_block(prev_non_empty):
                # Previous committed paragraph is also a quote → defer this one
                deferred.append(para)
                continue

        result.append(para)

        # After any non-empty, non-quote paragraph, release one deferred quote
        if deferred and para.strip() and not is_blockquote_block(para):
            result.append(deferred.pop(0))

    # Append any quotes that couldn't be re-inserted (e.g. article ends with non-quote content)
    result.extend(deferred)

    return '\n\n'.join(result)


def _strip_quote_wrapping(text: str) -> str:
    """Remove quote marks and Markdown emphasis around generated quote text."""
    text = text.strip()
    text = re.sub(r'^[*_]+|[*_]+$', '', text).strip()
    text = text.strip('"“”«»')
    text = re.sub(r'^[*_]+|[*_]+$', '', text).strip()
    return text


def _normalize_blockquote_for_medium(block: str) -> str:
    """Normalize one Markdown blockquote to Medium's preferred quote format."""
    raw_lines = [re.sub(r'^>\s?', '', line).strip() for line in block.split('\n')]
    non_empty_lines = [line for line in raw_lines if line]

    if not non_empty_lines:
        return block

    author = None
    author_match = re.match(r'^(?:—|–|-{1,2})\s*(.+)$', non_empty_lines[-1])
    if author_match:
        author = author_match.group(1).strip()
        quote_lines = non_empty_lines[:-1]
    else:
        quote_lines = non_empty_lines
        inline_match = re.match(r'^(.+?)\s+(?:—|–|-{1,2})\s+([^.!?\n]{2,80})$', quote_lines[-1])
        if inline_match:
            quote_lines[-1] = inline_match.group(1).strip()
            author = inline_match.group(2).strip()

    quote = _strip_quote_wrapping(' '.join(quote_lines))
    if not quote:
        return block

    normalized_lines = [f'> *{quote}*']
    if author:
        normalized_lines.extend(['>', f'> — {author}'])

    return '\n'.join(normalized_lines)


def normalize_quotes_for_medium(content: str) -> str:
    """Normalize all Markdown blockquotes to italic quote text plus separated attribution."""
    paragraphs = re.split(r'(\n{2,})', content)
    normalized = []

    for paragraph in paragraphs:
        stripped = paragraph.strip()
        if stripped and all(line.startswith('>') or not line.strip() for line in stripped.split('\n')):
            normalized.append(_normalize_blockquote_for_medium(paragraph))
        else:
            normalized.append(paragraph)

    return ''.join(normalized)


def remove_disallowed_em_dashes(content: str) -> str:
    """Remove em dashes everywhere except Medium quote attribution lines."""
    cleaned_lines = []

    for line in content.split('\n'):
        if re.match(r'^\s*>\s*—\s+\S', line):
            cleaned_lines.append(line)
        else:
            cleaned_lines.append(re.sub(r'\s*—\s*', ', ', line))

    return '\n'.join(cleaned_lines)


def clean_article_for_medium(content: str) -> str:
    """
    Clean article Markdown for optimal rendering on Medium.

    Fixes:
    - Converts ## subtitle right after # title to ### (smaller subtitle style)
    - Converts H4+ headings to bold (Medium only renders H1, H2, H3)
    - Strips trailing 'Kicker:' sections (GPT prompt leakage)
    - Collapses consecutive horizontal rules into one
    - Normalizes blockquotes for Medium
    - Removes em dashes except quote attribution markers
    - Trims excessive blank lines

    Note: The H1 title is intentionally KEPT in the content. The Medium API
    'title' field is used only for SEO/listing metadata — it does NOT appear
    on the actual post page. The title must be in the content body.
    """
    lines = content.split('\n')
    cleaned_lines = []
    h1_seen = False
    expect_subtitle = False

    for line in lines:
        stripped = line.strip()

        # Track blank lines right after H1 (before subtitle)
        if expect_subtitle and stripped == '':
            cleaned_lines.append(line)
            continue

        # Keep first H1 (it MUST be in the content for display on Medium)
        # but mark that we've seen it so the next heading can be a subtitle
        if not h1_seen and stripped.startswith('# ') and not stripped.startswith('## '):
            h1_seen = True
            expect_subtitle = True
            cleaned_lines.append(line)
            continue

        # Convert subtitle ## to ### if it appears right after the H1 title
        # Medium renders ## as a large section heading, ### is better for subtitles
        if expect_subtitle and stripped.startswith('## ') and not stripped.startswith('### '):
            cleaned_lines.append(f'### {stripped[3:]}')
            expect_subtitle = False
            continue

        if expect_subtitle and stripped != '':
            expect_subtitle = False

        # Convert H4+ headings to bold text (Medium only renders H1, H2, H3)
        if stripped.startswith('###### '):
            cleaned_lines.append(f'**{stripped[7:]}**')
            continue
        if stripped.startswith('##### '):
            cleaned_lines.append(f'**{stripped[6:]}**')
            continue
        if stripped.startswith('#### '):
            cleaned_lines.append(f'**{stripped[5:]}**')
            continue

        cleaned_lines.append(line)

    content = '\n'.join(cleaned_lines)

    # Remove trailing "Kicker:" sections (prompt leakage from GPT)
    content = re.sub(r'\n+###?\s*Kicker:\s*\n.*$', '', content, flags=re.DOTALL)

    # Normalize blockquote attribution lines for Medium.
    # Pattern 1: Author attribution outside blockquote (missing ">").
    #   > *Quote text*
    #   - Author
    # Fix: bring it inside the blockquote.
    content = re.sub(
        r'(^>\s*.+)\n\n?((?:—|–|-{1,2})\s*.+)$',
        r'\1\n>\n> \2',
        content,
        flags=re.MULTILINE
    )
    # Pattern 2: Quote and attribution on adjacent ">" lines with no blank ">" between.
    #   > *Quote text*
    #   > - Author
    # Add a blank ">" line so Medium renders them as separate visual lines in the same block.
    content = re.sub(
        r'(^>\s*[*_].+[*_])\n(>\s*(?:—|–|-{1,2})\s*)',
        r'\1\n>\n\2',
        content,
        flags=re.MULTILINE
    )
    content = normalize_quotes_for_medium(content)
    content = remove_disallowed_em_dashes(content)

    # Collapse consecutive --- separators
    content = re.sub(r'(---\s*\n\s*){2,}', '---\n\n', content)

    # Clean up excessive blank lines (more than 2 consecutive)
    content = re.sub(r'\n{4,}', '\n\n\n', content)

    # Ensure no two blockquote blocks sit directly next to each other
    content = separate_consecutive_quotes(content)

    return content.strip()


def convert_markdown_to_medium_html(content: str, title: str) -> str:
    """
    Convert cleaned Markdown article to Medium-compatible HTML.

    Medium's API accepts both 'html' and 'markdown' contentFormat, but HTML mode
    provides correct rendering for elements that Markdown mode handles poorly:

    - Image captions: <figure>/<figcaption> instead of unreliable italic text
    - Kicker: <h4> text above the <h1> title (Medium's kicker element)
    - Subtitle: <h3> text below the <h1> title
    - Title in content: the API 'title' field is SEO-only and does NOT appear
      on the actual post page; the title must be in the content as <h1>

    Args:
        content: Article content in Markdown format (already cleaned)
        title: Article title (will be ensured as H1 in content)

    Returns:
        str: Medium-compatible HTML content
    """
    # Step 1: Pre-process image+caption blocks to HTML figure/figcaption
    # BEFORE markdown conversion to prevent the library from wrapping them in <p>
    # Pattern: ![alt](url)\n*caption text with [links](href) and other content*
    def _image_caption_to_figure(match):
        alt = match.group(1)
        url = match.group(2)
        caption = match.group(3)
        # Convert any Markdown links [text](href) in caption to <a> tags
        caption_html = re.sub(
            r'\[([^\]]+)\]\(([^)]+)\)',
            r'<a href="\2">\1</a>',
            caption
        )
        return (
            f'\n\n<figure><img src="{url}" alt="{alt}">'
            f'<figcaption>{caption_html}</figcaption></figure>\n\n'
        )

    # Match: ![alt](url) followed by \n*caption* (italic caption line)
    content = re.sub(
        r'!\[([^\]]*)\]\(([^)]+)\)\s*\n\*([^*\n]+)\*',
        _image_caption_to_figure,
        content
    )

    # Handle standalone images without captions → figure without figcaption
    content = re.sub(
        r'!\[([^\]]*)\]\(([^)]+)\)',
        r'\n\n<figure><img src="\2" alt="\1"></figure>\n\n',
        content
    )

    # Step 2: Mark kicker for post-processing
    # Kicker = **bold text** immediately before # Title (the first H1)
    # Medium renders kicker as a small heading above the title (<h4>)
    content = re.sub(
        r'^\*\*([^*\n]+)\*\*\s*\n+(?=# [^#])',
        r'MEDIUM_KICKER_START\1MEDIUM_KICKER_END\n\n',
        content,
        count=1,
        flags=re.MULTILINE
    )

    # Step 3: Convert Markdown to HTML via the markdown library
    # 'extra' extension handles tables, fenced code, footnotes, etc.
    # 'sane_lists' prevents mixing of ordered/unordered lists
    html = md_lib.markdown(
        content,
        extensions=['extra', 'sane_lists'],
        output_format='html'
    )

    # Step 4: Post-process for Medium-specific formatting

    # Convert kicker placeholder to proper <h4> (Medium's kicker element)
    html = re.sub(
        r'<p>MEDIUM_KICKER_START(.+?)MEDIUM_KICKER_END</p>',
        r'<h4>\1</h4>',
        html
    )
    # Also handle case where markdown lib may include it differently
    html = html.replace('MEDIUM_KICKER_START', '<h4>').replace('MEDIUM_KICKER_END', '</h4>')

    # Catch any remaining unconverted patterns:
    # <p><img ...></p> followed by <p><em>caption</em></p> → figure/figcaption
    html = re.sub(
        r'<p>\s*<img([^>]*)>\s*</p>\s*<p>\s*<em>(.+?)</em>\s*</p>',
        r'<figure><img\1><figcaption>\2</figcaption></figure>',
        html,
        flags=re.DOTALL
    )

    # Catch img + em in same paragraph (single newline between image and caption)
    html = re.sub(
        r'<p>\s*<img([^>]*)>\s*<br\s*/?>\s*\n?\s*<em>(.+?)</em>\s*</p>',
        r'<figure><img\1><figcaption>\2</figcaption></figure>',
        html,
        flags=re.DOTALL
    )

    # Convert any remaining bare <p><img ...></p> to <figure> (no caption)
    html = re.sub(
        r'<p>\s*<img([^>]*)>\s*</p>',
        r'<figure><img\1></figure>',
        html
    )

    # Ensure the title appears in content as H1
    # The Medium API 'title' field is used only for SEO/listing — it does NOT
    # appear on the actual post page, so the H1 must be in the content body.
    if not re.search(r'<h[1][\s>]', html[:500]):
        # Title is missing from content — add it at the top (after kicker if present)
        kicker_match = re.match(r'(\s*<h4>.*?</h4>\s*)', html)
        if kicker_match:
            # Insert H1 right after the kicker
            insert_pos = kicker_match.end()
            html = html[:insert_pos] + f'\n<h1>{title}</h1>\n' + html[insert_pos:]
        else:
            html = f'<h1>{title}</h1>\n' + html

    return html


def save_article_locally(
        video_id: str,
        original_title: str,
        title: str,
        tags: List[str],
        article: str,
        medium_url: str,
        base_dir: str = 'articles',
        published_urls: Optional[Dict[str, str]] = None
) -> str:
    """
    Save the generated article locally as a Markdown file.

    Args:
        video_id (str): The unique video ID from YouTube
        original_title (str): The original title from the video
        title (str): The optimized title for the article
        tags (List[str]): List of tags for the article
        article (str): The content of the article in Markdown format
        medium_url (str): The primary/canonical published URL (or "not_published")
        base_dir (str, optional): Base directory for saving articles. Defaults to 'articles'
        published_urls (Optional[Dict[str, str]]): Map of platform name -> published URL

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
    file_name: str = os.path.join(base_dir, f"{video_id}_{safe_title}.md")

    # Check if article already exists
    if os.path.exists(file_name):
        return file_name

    # Format tags with comma and space separation
    formatted_tags: str = ', '.join(tags)

    # Generate YouTube video URL
    youtube_url: str = f"https://www.youtube.com/watch?v={video_id}"

    # Build per-platform published URL lines (one line per platform that succeeded)
    platform_lines: str = ""
    if published_urls:
        for platform, url in published_urls.items():
            if url:
                platform_lines += f"{platform}_url: {url}\n"

    metadata_header: str = f"""---
video_id: {video_id}
youtube_url: {youtube_url}
original_title: {original_title}
optimized_title: {title}
medium_url: {medium_url}
{platform_lines}date: {datetime.now().isoformat()}
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
        print(f"✗ Error saving article: {e}")
        raise

    print(f"✓ Article saved locally at: {file_name}")
    return file_name

def build_medium_html(markdown_content: str, title: str) -> str:
    """
    Convert a Markdown article into Medium-compatible HTML.

    Used by the Medium publisher. HTML contentFormat is preferred because it
    renders elements that Markdown mode handles poorly:
    - Image captions via <figure>/<figcaption>
    - Kicker text via <h4> above the <h1> title
    - Subtitle via <h3> below the <h1> title
    - Title display on the post page (API 'title' field is SEO-only)
    """
    cleaned_md = clean_article_for_medium(markdown_content)
    return convert_markdown_to_medium_html(cleaned_md, title)

def check_article_exists(video_id: str, original_title: str, base_dir: str = 'articles') -> Optional[str]:
    """
    Check if an article already exists locally based on the video ID and title.

    Args:
        video_id (str): The unique video ID from YouTube
        original_title (str): The title of the article
        base_dir (str, optional): The base directory to search for articles. Defaults to 'articles'.

    Returns:
        Optional[str]: The path of the existing article file or None if it doesn't exist.
    """
    safe_title = "".join([c for c in original_title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    file_name = os.path.join(base_dir, f"{video_id}_{safe_title}.md")
    return file_name if os.path.exists(file_name) else None


def check_unpublished_article(video_id: str, original_title: str, base_dir: str = 'articles') -> Optional[str]:
    """
    Check if an unpublished article exists locally for the given video ID and title.

    Args:
        video_id (str): The unique video ID from YouTube
        original_title (str): The original video title
        base_dir (str, optional): The base directory to search. Defaults to 'articles'.

    Returns:
        Optional[str]: Path to the unpublished article file or None if not found
    """
    safe_title = "".join([c for c in original_title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
    unpublished_file = os.path.join(base_dir, f"not_published_{video_id}_{safe_title}.md")
    return unpublished_file if os.path.exists(unpublished_file) else None


def extract_article_from_file(file_path: str) -> Optional[Dict[str, Any]]:
    """
    Extract article content and metadata from a saved markdown file.

    Args:
        file_path (str): Path to the markdown file

    Returns:
        Optional[Dict[str, Any]]: Dictionary containing 'title', 'tags', 'content', or None if parsing fails
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Split metadata from article content
        parts = content.split('---\n')
        if len(parts) < 3:
            print(f"✗ Invalid article format in {file_path}")
            return None
        
        metadata_section = parts[1]
        article_content = '---\n'.join(parts[2:]).strip()
        
        # Parse metadata
        metadata = {}
        for line in metadata_section.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                metadata[key.strip()] = value.strip()
        
        # Extract required fields
        optimized_title = metadata.get('optimized_title', '')
        tags_str = metadata.get('tags', '')
        tags = [tag.strip() for tag in tags_str.split(',') if tag.strip()]
        
        if not optimized_title or not article_content:
            print(f"✗ Missing required fields in {file_path}")
            return None
        
        return {
            'title': optimized_title,
            'tags': tags,
            'content': article_content
        }
    
    except Exception as e:
        print(f"✗ Error reading article from {file_path}: {e}")
        return None


def rename_published_article(old_path: str, video_id: str, original_title: str, base_dir: str) -> Optional[str]:
    """
    Rename an unpublished article file to a published one after successful Medium post.

    Args:
        old_path (str): Current path of the unpublished article
        video_id (str): The unique video ID from YouTube
        original_title (str): Original video title (without 'not_published_' prefix)
        base_dir (str): Base directory where the file is located

    Returns:
        Optional[str]: New file path or None if rename fails
    """
    try:
        safe_title = "".join([c for c in original_title if c.isalpha() or c.isdigit() or c == ' ']).rstrip()
        new_path = os.path.join(base_dir, f"{video_id}_{safe_title}.md")
        
        os.rename(old_path, new_path)
        print(f"✓ Renamed article: {os.path.basename(old_path)} → {os.path.basename(new_path)}")
        return new_path
    
    except Exception as e:
        print(f"✗ Error renaming article: {e}")
        return None


def update_article_medium_url(file_path: str, medium_url: str) -> bool:
    """
    Update the medium_url field in an article's metadata.

    Args:
        file_path (str): Path to the article file
        medium_url (str): New Medium URL to set

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Replace the medium_url in metadata
        updated_content = content.replace('medium_url: not_published', f'medium_url: {medium_url}')
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)
        
        return True
    
    except Exception as e:
        print(f"✗ Error updating medium_url in {file_path}: {e}")
        return False


def process_niche(youtube, niche_name: str, niche_config: Dict[str, Any], publishers: List[Any]):
    """
    Process videos for a specific niche.
    
    Args:
        youtube: YouTube API service instance
        niche_name: Name of the niche ('self-help' or 'tech')
        niche_config: Configuration dictionary for the niche
        publishers: List of configured platform publishers (Medium, Dev.to, ...)
    """
    channel_id = niche_config['YOUTUBE_CHANNEL_ID']
    source_language = niche_config.get('SOURCE_LANGUAGE', 'en')
    output_languages = niche_config.get('OUTPUT_LANGUAGES', ['en'])
    base_dir = niche_config.get('ARTICLES_BASE_DIR', 'articles')
    
    print(f"\n{'='*60}")
    print(f"Processing niche: {niche_name.upper()}")
    print(f"Channel ID: {channel_id}")
    print(f"Source language: {source_language}")
    print(f"Output languages: {', '.join(output_languages)}")
    print(f"{'='*60}\n")
    
    videos = get_channel_videos(youtube, channel_id)
    print(f"Found {len(videos)} videos in the {niche_name} channel")

    for index, video in enumerate(videos, 1):
        try:
            # Quick check: if article exists for all output languages, skip without waiting
            all_exist = True
            for output_language in output_languages:
                # Determine article directory
                if niche_name == 'self-help' and output_language != 'en':
                    article_dir = f"{base_dir}/{output_language}"
                else:
                    article_dir = base_dir
                
                # Check if article exists or if unpublished version exists
                if not (check_article_exists(video.id, video.title, base_dir=article_dir) or
                        check_unpublished_article(video.id, video.title, base_dir=article_dir)):
                    all_exist = False
                    break
            
            # If all articles exist, skip immediately without rate limiting
            if all_exist:
                print(f"[{index}/{len(videos)}] Already exists locally. Skipping '{video.title}' (no wait)")
                continue
            
            # Apply rate limiting only for videos that need processing
            print(f"Waiting for {RATE_LIMIT_PERIOD_SECONDS} seconds before processing the next video...")
            print_progress_separator(index, len(videos), video.title)
            
            # Process for each output language
            for output_language in output_languages:
                try:
                    # Determine article directory
                    if niche_name == 'self-help' and output_language != 'en':
                        article_dir = f"{base_dir}/{output_language}"
                    else:
                        article_dir = base_dir
                    
                    # Skip if article already exists and is published
                    if check_article_exists(video.id, video.title, base_dir=article_dir):
                        print(f"Already exists locally. Skipping '{video.title}' for {output_language}")
                        continue
                    
                    # Check for unpublished article and try to publish it first (saves OpenAI credits)
                    unpublished_path = check_unpublished_article(video.id, video.title, base_dir=article_dir)
                    if unpublished_path:
                        print(f"✓ Found unpublished article: {os.path.basename(unpublished_path)}")
                        print(f"✓ Attempting to publish existing article (avoiding OpenAI regeneration)...")
                        
                        article_data = extract_article_from_file(unpublished_path)
                        if article_data:
                            try:
                                results = publish_to_all(
                                    publishers,
                                    title=article_data['title'],
                                    content=article_data['content'],
                                    tags=article_data['tags'],
                                    output_language=output_language,
                                    niche=niche_name
                                )
                                primary_url = select_primary_url(results)

                                if any(r.success for r in results.values()):
                                    print(f"✓ Successfully published! URL: {primary_url}")
                                    # Update the primary published URL in the file
                                    if update_article_medium_url(unpublished_path, primary_url):
                                        # Rename file to remove 'not_published_' prefix
                                        new_path = rename_published_article(unpublished_path, video.id, video.title, article_dir)
                                        if new_path:
                                            print(f"✓ Article optimization complete - saved OpenAI API credits!")
                                            continue
                                else:
                                    print(f"✗ Publication failed again, will keep as unpublished")
                                    continue
                            except Exception as e:
                                print(f"✗ Error publishing existing article: {e}")
                                continue
                    
                    # If no unpublished article exists, generate new content from transcript
                    transcript = get_video_transcript(video.id, language=source_language)
                    if not transcript:
                        print(f"No transcript available for: {video.title}")
                        continue
                    
                    article = generate_article_from_transcript(
                        transcript,
                        video.title,
                        source_language=source_language,
                        output_language=output_language,
                        video_duration=video.duration_seconds,
                        niche=niche_name
                    )
                    
                    tags = generate_tags(article, video.title, output_language=output_language, niche=niche_name)
                    optimized_title = generate_article_title(article, output_language=output_language)

                    # Retrieve images. Number of images depends if the article is long or short
                    images_per_article = 4 if len(article) > VERY_LONG_ARTICLE_THRESHOLD else (
                        3 if len(article) > LONG_ARTICLE_THRESHOLD else 2)

                    # Step 1 — Generate targeted visual search queries (one per image slot).
                    # These are crafted from the article's content so every image is
                    # topically and emotionally relevant, not just a generic keyword match.
                    visual_queries = generate_unsplash_search_queries(
                        article_title=optimized_title,
                        article_snippet=article[:500],
                        tags=tags,
                        num_images=images_per_article,
                        output_language=output_language
                    )

                    # Step 2 — Fetch one distinct image per query for a varied, curated set.
                    images = fetch_images_for_article(
                        queries=visual_queries,
                        article_title=optimized_title,
                        output_language=output_language
                    )

                    if images:
                        # Step 3 — Generate unique, article-specific caption descriptions.
                        # Replaces the raw Unsplash alt_description with something that
                        # ties each image directly to this article's message.
                        unique_caption_descs = generate_unique_image_captions(
                            images=images,
                            article_title=optimized_title,
                            article_snippet=article[:400],
                            output_language=output_language
                        )

                        # Step 4 — Apply unique captions to images, preserving photographer credit.
                        # The photographer attribution part is extracted from the existing caption
                        # and reused; only the descriptive portion is replaced.
                        photo_credit_patterns = {
                            'en': r'(Photo by \[.+?\]\(.+?\))',
                            'fr': r'(Photo de \[.+?\]\(.+?\))',
                        }
                        credit_pattern = photo_credit_patterns.get(output_language, photo_credit_patterns['en'])
                        for image, new_desc in zip(images, unique_caption_descs):
                            credit_match = re.search(credit_pattern, image.caption)
                            if credit_match and new_desc:
                                image.caption = f"{new_desc} - {credit_match.group(1)}"
                                image.alt = new_desc

                        article = embed_images_in_content(article, images, optimized_title)

                    # Embed YouTube video for tech niche only
                    if niche_name == 'tech':
                        article = embed_youtube_video(article, video.id)
                        print(f"✓ Embedded YouTube video in article")

                    # Clean Markdown for local save (fix unsupported headings, blockquotes, etc.)
                    # Note: the Medium publisher also cleans + converts to HTML internally
                    article = clean_article_for_medium(article)

                    # Publish to every configured platform (Medium, Dev.to, Hashnode, ...).
                    # Saving locally still happens even if all publishers fail.
                    published_urls: Dict[str, str] = {}
                    try:
                        results = publish_to_all(
                            publishers,
                            title=optimized_title,
                            content=article,
                            tags=tags,
                            output_language=output_language,
                            niche=niche_name
                        )
                        published_urls = {
                            name: r.url for name, r in results.items() if r.success and r.url
                        }
                        medium_url = select_primary_url(results)
                        if medium_url not in ("not_published", "posted_as_draft"):
                            print(f"✓ Article available at: {medium_url}")
                    except Exception as e:
                        print(f"✗ Failed to publish article: {e}")
                        medium_url = "not_published"

                    save_article_locally(
                        video.id,
                        "not_published_" + video.title if medium_url == "not_published" else video.title,
                        optimized_title,
                        tags,
                        article,
                        medium_url,
                        base_dir=article_dir,
                        published_urls=published_urls
                    )

                except Exception as e:
                    print(f"✗ Error processing video {video.title} for {output_language}: {e}")
        
        except Exception as e:
            print(f"✗ Error processing video {video.title}: {e}")

def main():
    """
    Main function to process all configured niches.
    """
    youtube = get_authenticated_service()

    # Build the set of enabled publishers once (Medium, Dev.to, Hashnode, ...).
    # The Medium publisher needs the Markdown -> HTML converter from this module.
    publishers = build_publishers(config, build_medium_html)

    niches_config = config.get('NICHES', {})
    active_niche = config.get('ACTIVE_NICHE', 'all')

    if not niches_config:
        print("✗ No niches configured in config.json")
        return

    # Determine which niches to process
    if active_niche == 'all':
        niches_to_process = niches_config.items()
    elif active_niche in niches_config:
        niches_to_process = [(active_niche, niches_config[active_niche])]
    else:
        print(f"✗ Invalid ACTIVE_NICHE: {active_niche}")
        return

    # Process each niche
    for niche_name, niche_config in niches_to_process:
        try:
            process_niche(youtube, niche_name, niche_config, publishers)
        except Exception as e:
            print(f"✗ Error processing {niche_name} niche: {e}")
            continue

    print("\n✓ All niches processed successfully!")

if __name__ == "__main__":
    main()
