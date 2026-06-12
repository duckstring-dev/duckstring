STATIC_DIR := src/duckstring/catchment/static
FRONTEND_OUT := frontend/out

.PHONY: dev build-frontend clean-frontend

dev:
	duckstring catchment init --name dev --root .dev --port 8000

build-frontend: clean-frontend
	cd frontend && NEXT_STATIC_EXPORT=true npx next build
	cp -r $(FRONTEND_OUT)/. $(STATIC_DIR)/

clean-frontend:
	find $(STATIC_DIR) -mindepth 1 -maxdepth 1 ! -name 'dev' -exec rm -rf {} +
