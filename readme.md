# YouTube to Medium Automation

**Automate the process of converting YouTube video content into Medium articles 🎥📝**

This program not only converts video transcripts extremely well into articles, but also removes any "transcript-like" writing. It produces a real, professional article from a video instead.

- [⚙️ Requirements](#%EF%B8%8F-requirements)
- [📦 Installation](#-installation)
- [🪄 Usage](#-usage)
- [🛠️ Configuration](#%EF%B8%8F-configuration)
- [👨‍🍳 Who is the creator?](#-who-created-this)
- [🤝 Contributing](#-contributing)
- [⚖️ License](#%EF%B8%8F-license)

## ⚙️ Requirements
 * [Python v3.7](https://www.python.org/downloads/) or higher 🐍
 * A Google account with YouTube API access 🎬
 * An OpenAI API key 🧠
 * A Medium account with an integration token ✍️

## 📦 Installation

1. Clone this repository:
   ```console
   git clone https://github.com/pH-7/youtube-to-medium-blog-posts-automation.git
   cd youtube-to-medium-blog-posts-automation
   ```

2. Install the required Python packages:
   ```console
   pip install -r requirements.txt
   ```

3. Set up your configuration file:
   - Create a file named `config.json` in the project root directory
   - Add your API keys and YouTube channel ID to the file as followed:
     ```json
     {
       "YOUTUBE_API_KEY":     "YOUR_YOUTUBE_API_KEY",
       "OPENAI_API_KEY":      "YOUR_OPENAI_API_KEY",
       "MEDIUM_ACCESS_TOKEN": "YOUR_MEDIUM_ACCESS_TOKEN",
       "YOUTUBE_CHANNEL_ID":  "YOUR_CHANNEL_ID",
       "SOURCE_LANGUAGE":     "fr"
     }
     ```

     Alternatively, you can refer to `example.config.json` in the project.

4. Set up YouTube API credentials:
   - Go to the [Google Developers Console](https://console.developers.google.com/)
   - Create a new project and enable the YouTube Data API v3
   - Create credentials (OAuth 2.0 Client ID) for a desktop application. Select **External** for **Use Type**
   - Download the client configuration and save it as `client_secrets.json` in the project root directory

## 🪄 Usage

To run the script, use the following command in the project root directory:

```console
python youtube_to_medium_script.py
```

**The script will:**
1. Fetch recent videos from your YouTube channel
2. Transcribe each video
3. Generate an article for each transcription
4. Create relevant tags for the article
5. Post the article to Medium as a draft

**Note:** The script posts articles as drafts by default. To change this, modify the `publishStatus` in the `post_to_medium` function.

## 🛠️ Configuration

You can modify the following in the script:
- The number of videos to process (change `maxResults` in `get_channel_videos`)
- The length of the generated article (change `max_tokens` in `generate_article`)
- The number of tags to generate (modify the prompt in `generate_tags`)

## 👨‍🍳 Who created this?

[![Pierre-Henry Soria](https://s.gravatar.com/avatar/a210fe61253c43c869d71eaed0e90149?s=200)](https://PH7.me 'Pierre-Henry Soria personal website')

**Pierre-Henry Soria**. A passionate developer who loves automating content creation! 🚀 Enthusiast for YouTube, AI, and writing! 😊 Find me at [PH7.me](https://PH7.me) 💫


☕️ Enjoying this project? **[Offer me a coffee](https://ko-fi.com/phenry)** and fuel more awesome automations! 💪

[![@phenrysay][twitter-image]](https://x.com/phenrysay) [![pH-7][github-image]](https://github.com/pH-7)


## 🤝 Contributing

Contributions to this project are welcome. Please fork the repository and submit a pull request with your changes.

## ⚖️ License

**YouTube to Medium Automation** is generously distributed under the *[MIT License](https://opensource.org/licenses/MIT)* 🎉 Enjoy!

## ⚠️ Disclaimer

This project is for educational purposes only. Ensure you comply with YouTube's terms of service and Medium's API usage guidelines when using this script.

<!-- GitHub's Markdown reference links -->
[twitter-image]: https://img.shields.io/badge/Twitter-1DA1F2?style=for-the-badge&logo=twitter&logoColor=white
[github-image]: https://img.shields.io/badge/GitHub-100000?style=for-the-badge&logo=github&logoColor=white