# Apify's Playwright image already carries Chromium and Xvfb, which this actor
# needs: Naukri's bot protection blocks headless Chromium, so the browser runs
# headed against a virtual display rather than with --headless.
FROM apify/actor-python-playwright:3.11

COPY requirements.txt ./

RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir -r requirements.txt \
    && echo "All installed:" \
    && pip freeze

COPY . ./

# xvfb-run gives the headed browser a display. Without it Chromium exits
# immediately with "Missing X server or $DISPLAY".
CMD ["xvfb-run", "-a", "python", "main.py"]
