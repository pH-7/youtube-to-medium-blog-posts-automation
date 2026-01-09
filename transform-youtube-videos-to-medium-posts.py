import os
import json
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
            "🤖 Get inspired by [open-source projects I've built](https://github.com/pH-7) over the years",
            "🔥 Follow my [AI & tech journey on Substack](https://substack.com/@pierrehenry)",
            "⚡️ Check out [my book on PRO coding practices](https://github.com/pH-7/GoodJsCode)",
            "🤔 [Learn more about me on Dev.to](https://dev.to/pierre)",
            "👋 [Support my work with a coffee](https://ko-fi.com/phenry) if this helped you",
            "📺 [Subscribe to my YouTube channel](https://www.youtube.com/@pH7Programming) for weekly programming videos"
        ]
        
        # Randomly select 2-3 CTAs
        num_ctas = random.randint(2, 3)
        selected_ctas = random.sample(tech_ctas, num_ctas)
        tech_cta_section = '\n'.join(selected_ctas)
        
        prompts = {
            'en': f"""{{instruction}} Remove all filler sounds and verbal tics.
    Rewrite this as a well-structured technical article for "NextGen Dev: AI & Software Development", skipping video intro/outro and promotional content.
    
    CRITICAL: Preserve the speaker's EXACT voice, tone, and personality. Match their speaking style precisely:
    - Keep their casual/formal tone, first-person perspective, directness, and enthusiasm
    - Maintain their teaching style, analogies, and personal experiences
    - Avoid generic corporate tech blog voice - write as the speaker would write
    
    For longer content, develop technical concepts with code examples and practical insights. Create natural narrative flow.
    End with "Key Takeaways" bullet points. Include 1-2 relevant technical quotes if appropriate.
    
    After a Markdown separator, add this CTA section in the same voice as the article:
    {tech_cta_section}

    Kicker: Short article kicker as subheading
    Title: {title}
    Subtitle: Optional concise technical subtitle as subheading

    Transcript: {transcript_to_use}

    Format as Medium.com article. Use clear technical language matching the speaker's natural style.
    DO NOT use em dashes. Avoid unnecessary buzzwords and corporate jargon unless the speaker uses them. Highlight a few important sentences if any.
    Use Markdown for headings, code blocks, links, bold, italic:"""
        }
    else:  # self-help niche
        prompts = {
            'en': f"""{{instruction}} Remove all filler sounds like "euh...", "bah", "ben", "hein" and similar verbal tics.
    While ensuring em dashes aren't used, rewrite it as a well-structured, comprehensive article in English, skipping the video introduction (e.g. Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), the ending section (e.g. au revoir, code de promotion, code de réduction, je te retouve dans mes formations, à bientôt, ciao, n'oublie pas de t'abonner, ...), CTA related to PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com and workshops.
    Ensure it reads well and doesn't sound like a transcript, though the article must keep the exact same personal, positive, and motivational voice tone and unique written style markers as the transcript, and emphasise or highlight personal ideas that could fascinate the readers. Pay special attention to French idioms and expressions, translating them to their natural English equivalents.
    For longer content, develop each key concept thoroughly with examples, actionable steps, and deeper insights. Create a cohesive narrative that flows naturally from one idea to the next.
    End the article with short bullet/numbered points of a TL;DR / Key Takeaways or Key Lessons, Actions List, and/or "What About You ?" / "Ask Yourself" styled questions in italic font preceded by Markdown separator.
    If relevant to article's theme, include 1 to 3 impactful quotes in different places throughout the article that deeply resonate with the article's message. Format each quote in Markdown using blockquote syntax (>) in italic font without surrounding quotation marks, followed by the author's name on a separate line, preceded by an em dash.
    Lastly, in the exact same personal voice tone as the transcript, lead readers to read my complementary book available at https://book.ph7.me (use anchor text such as "my self-help guide" and emphasize/bold it). Suggest my podcast https://podcasts.ph7.me co-hosted with El, and/or invite them subscribe to my private mailing list at https://masterclass.ph7.me (always use anchor text for links), preceded by another Markdown separator.

    Kicker: Right before Title, very short article's kicker formatted as subheading.
    Title: {title}
    Subtitle: Right after Title, optional concise appealing / clickbait formatted as subheading.

    Transcript: {transcript_to_use}

    Structured as a Medium.com article in English while keeping the identical same voice tone as in the original transcript.
    Use simple words, no em dashes, and DO NOT use any unnecessary or complicated adjective such as: Unlock, Effortless, Explore, Insights, Today's Digital World, In today's world, Dive into, Refine, Evolving, Embrace, Embracing, Embark, Enrich, Envision, Unleash, Unmask, Unveil, Streamline, Fast-paced, Delve, Digital Age, Game-changer, Indulge, Merely, Endure.
    Use Markdown format for headings, links, bold, italic, etc:""",

        }
        if output_language == 'fr':  # Only self-help niche has French output
            prompts['fr'] = f"""{{instruction}} en supprimant les sons de remplissage comme "euh...", "bah", "ben", "hein" et autres tics verbaux similaires.
    Réécris-le sous forme d'article bien structuré en français, en omettant l'introduction vidéo (ex: Bonjour à toi, Comment vas-tu, Bienvenue sur ma chaîne, ...), la conclusion (ex: au revoir,  code de promotion, code de réduction, je te retouve dans mes formations, à bientôt, ciao, N'oublie pas de t'abonner, ...), et exclus toute promotion liée à PIERREWRITER.COM, pier.com, pwrit.com, prwrit.com et aux ateliers.
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
    Si cela est pertinent avec l'article, inclue 1 à 3 citations dispercées dans l'article et percutantes qui résonnent profondément avec le message de l'article. Formate chaque citation en Markdown blockquote en utilisant (>) et en italique, sans entourer la citation entre guillemets, puis ajoute le nom de l'auteur sur une ligne séparée, précédé d'un tiret cadratin.
    Termine l'article avec un bref récap sous forme de points et/ou liste d'actions que le lecteur peut directement appliquer, précédé d'un séparateur Markdown.
    Enfin, suggérer le lecteur de s'inscrire à ma liste de contacts sur https://contacts.ph7.me (utilise un texte d'ancrage), précédé d'un séparateur Markdown.

    Kicker: Juste avant le Titre, très courte phrase d'accroche optionnelle en police h3.
    Titre: {title}
    Sous-titre: Juste après le Titre, sous-titre optionnel en police h3, qui donne une promesse concise qui aguiche/intrigue davantage.

    Transcription: {transcript_to_use}

    Structure le texte en tant qu'article Medium.com français tout en gardant le même ton de voix que dans la transcription, utilise le tutoiement et prioritise les mots simples. Utilise le format Markdown pour les titres, liens, gras, italique, etc:"""

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
        'en': f"""Based on the content provided below, generate an engaging title for a Medium.com article.

    Content: {article_content[:550]}  # Limit the content sent to the model
    
    Ensure the title grabs attention and intrigues readers to absolutely read the article. The title should be creative and concise, ideally under 60 characters.
    When it relevant, use one of these following title formats: "Use/Adopt [Skill|Action] or [Bad Consequence]", "How [Action|Benefit] WITHOUT [Related Pain Point]?", "How to [Action|Benefit] in [Limited Time]?", "The New Way to [Action] With No [Friction Point]".

    Do not use em dashes, hyphens, or dashes. Avoid irrelevant adjective like Unlock, Effortless, Evolving, Embrace, Enrich, Unleash, Unmask, Unveil, Streamline, Fast-paced, Game-changer, ... and prioritize simple words.""",

        'fr': f"""À partir du contenu fourni ci-dessous, génère un titre accrocheur pour un article Medium.com.

    Contenu: {article_content[:550]}  # Limite le contenu envoyé au modèle
    
    Assure-toi que le titre attire l'attention des lecteurs. Le titre doit être créatif et concis, idéalement moins de 60 caractères.
    Dans la mesure du possible, utilise l'un des formats suivants : "Comment [Action|Bénéfice] SANS [Point de Douleur] ?", "Comment [Action|Bénéfice] en [Temps Limité] ?", "La Nouvelle Façon de [Action] SANS [Point de Friction]", "Faites [Compétence/Action] ou [Conséquence]".
    N'utilise pas de tirets cadratins, tirets, ou traits d'union. Utilise le tutoiement et utilise des mots simples. N'utilise aucun adjectif non pertinent ou compliqué comme Débloquer, Dévoiler, Démasquer, Révéler, Rationaliser, Révolutionnaire."""
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

    title = response.choices[0].message.content.strip('"')
    print(f"✓ Generated article title: {title}")
    return title

def fetch_images_from_unsplash(query, article_title: str, output_language: str = 'en', per_page: int = 2) -> Optional[List[UnsplashImage]]:
    """
    Fetch images from Unsplash with recursive fallback for better search results.
    
    Args:
        query: Can be a string or list of tags
        article_title: The title of the article to use in captions
        output_language: Target language ('en' or 'fr')
        per_page: Number of images to fetch (default: 2)
    Returns:
        Optional[List[UnsplashImage]]: List of UnsplashImage objects with URLs, alt text, and attribution captions in Markdown
    """
    if isinstance(query, list):
        # If it's a list of tags, join the first 3
        search_query = ' '.join(query[:3])
    else:
        # If it's a string, use it as is
        search_query = query
    
    unsplash_access_key = config['UNSPLASH_ACCESS_KEY']
    preferred_photographer = config.get('UNSPLASH_PREFERRED_PHOTOGRAPHER')
    results = []
    
    try:
        print(f"✓ Fetching images from Unsplash for query: '{search_query}'")

        # If preferred photographer is configured, try to get their images first
        if preferred_photographer:
            # Search for images by preferred photographer with the query
            user_search_url = (
                f"https://api.unsplash.com/search/photos"
                f"?query={search_query}"
                f"&username={preferred_photographer}"
                f"&client_id={unsplash_access_key}"
                f"&per_page={per_page}"
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
                f"&orientation=landscape"
            )
            
            search_response = requests.get(search_url)
            search_response.raise_for_status()
            search_results = search_response.json()

            search_photos = search_results.get('results', [])
            results.extend(search_photos)
            print(f"✓ Fetched {len(search_photos)} additional images from general search")

        # If no results found and we have a complex query, try with fewer keywords
        if not results and isinstance(query, list) and len(query) > 1:
            print(f"✗ No images found for '{search_query}'. Trying with fewer keywords...")
            # Try with fewer tags recursively
            return fetch_images_from_unsplash(
                query=query[:-1],  # Remove the last tag
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

        results = results[:per_page]  # Ensure we don't exceed the requested number of images

        if not results:
            print(f"✗ No images found for any search variation")
            return None

        captions = {
            'en': lambda name, photo_url, profile_url: f"{article_title} - Photo by [{name}]({profile_url}) on [Unsplash]({photo_url})",
            'fr': lambda name, photo_url, profile_url: f"{article_title} - Photo de [{name}]({profile_url}) sur [Unsplash]({photo_url})"
        }

        caption_formatter = captions.get(output_language, captions['en'])

        processed_images = [
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
        
        print(f"✓ Successfully processed {len(processed_images)} images")
        return processed_images if processed_images else None
        
    except Exception as e:
        print(f"✗ Failed to fetch images from Unsplash: {e}")
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

def save_article_locally(
        video_id: str,
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
        video_id (str): The unique video ID from YouTube
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
    file_name: str = os.path.join(base_dir, f"{video_id}_{safe_title}.md")

    # Create directory if it doesn't exist
    os.makedirs(base_dir, exist_ok=True)

    # Check if article already exists
    if os.path.exists(file_name):
        return file_name

    # Format tags with comma and space separation
    formatted_tags: str = ', '.join(tags)

    # Generate YouTube video URL
    youtube_url: str = f"https://www.youtube.com/watch?v={video_id}"

    metadata_header: str = f"""---
video_id: {video_id}
youtube_url: {youtube_url}
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
        print(f"✗ Error saving article: {e}")
        raise

    print(f"✓ Article saved locally at: {file_name}")
    return file_name

def post_to_medium(title: str, content: str, tags: List[str], output_language: str, niche: str = 'self-help') -> Optional[str]:
    """
    Post article to Medium with support for publication posting.
    """
    config = load_config()
    en_publication_id = config.get('MEDIUM_EN_PUBLICATION_ID')
    fr_publication_id = config.get('MEDIUM_FR_PUBLICATION_ID')
    tech_publication_id = config.get('MEDIUM_TECH_PUBLICATION_ID')
    post_to_publication = config.get('POST_TO_PUBLICATION', False)
    token = config['MEDIUM_ACCESS_TOKEN']
    publish_status = config['PUBLISH_STATUS']

    # Prepare article in Markdown format
    # Note: Medium API handles title separately, so we don't include it in content
    article = {
        "title": title,
        "contentFormat": "markdown",
        "content": content,
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
        # Select publication based on niche and language
        if niche == 'tech':
            publication_id = tech_publication_id
        else:
            publication_id = fr_publication_id if output_language == 'fr' else en_publication_id

        # Only post to publication if we have a valid publication ID
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

        print(f"✓ Article posted to Medium.com")
        return response.json()["data"]["url"]

    except Exception as e:
        print(f"✗ Failed to post article: {e}")
        print(f"Response: {response.text if 'response' in locals() else 'No response'}")
        return None

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


def process_niche(youtube, niche_name: str, niche_config: Dict[str, Any]):
    """
    Process videos for a specific niche.
    
    Args:
        youtube: YouTube API service instance
        niche_name: Name of the niche ('self-help' or 'tech')
        niche_config: Configuration dictionary for the niche
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
                        print(f"✓ Attempting to publish existing article to Medium (avoiding OpenAI regeneration)...")
                        
                        article_data = extract_article_from_file(unpublished_path)
                        if article_data:
                            try:
                                medium_url = post_to_medium(
                                    article_data['title'],
                                    article_data['content'],
                                    article_data['tags'],
                                    output_language,
                                    niche=niche_name
                                )
                                
                                if medium_url:
                                    print(f"✓ Successfully published! URL: {medium_url}")
                                    # Update the medium_url in the file
                                    if update_article_medium_url(unpublished_path, medium_url):
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

                    # Fetch images from Unsplash using the tags
                    images = fetch_images_from_unsplash(
                        query=tags,
                        article_title=optimized_title,
                        output_language=output_language,
                        per_page=images_per_article
                    )
                    if images:
                        article = embed_images_in_content(article, images, optimized_title)

                    # Embed YouTube video for tech niche only
                    if niche_name == 'tech':
                        article = embed_youtube_video(article, video.id)
                        print(f"✓ Embedded YouTube video in article")

                    # Set default medium_url
                    medium_url = "not_published"

                    # Try to post to Medium, but continue saving article locally if fails
                    try:
                        medium_result = post_to_medium(optimized_title, article, tags, output_language, niche=niche_name)
                        if medium_result:
                            medium_url = medium_result
                            print(f"✓ Article available at: {medium_url}")
                    except Exception as e:
                        print(f"✗ Failed to post to Medium: {e}")

                    save_article_locally(
                        video.id,
                        "not_published_" + video.title if medium_url == "not_published" else video.title,
                        optimized_title,
                        tags,
                        article,
                        medium_url,
                        base_dir=article_dir
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
            process_niche(youtube, niche_name, niche_config)
        except Exception as e:
            print(f"✗ Error processing {niche_name} niche: {e}")
            continue

    print("\n✓ All niches processed successfully!")

if __name__ == "__main__":
    main()
