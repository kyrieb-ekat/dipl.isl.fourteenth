"""UI modules for the DI charter review app.

Deliberately NOT named `pages/`: Streamlit auto-discovers a `pages/`
directory sitting next to the entrypoint script and builds its own automatic
navigation from it, which would compete with the explicit
st.navigation()/st.Page() registry in review_app.py.
"""
