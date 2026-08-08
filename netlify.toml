[build]
  publish = "."

[functions]
  directory = "netlify/functions"
  node_bundler = "esbuild"
  included_files = ["app.py"]

[[redirects]]
  from = "/*"
  to = "/.netlify/functions/flask_api"
  status = 200
