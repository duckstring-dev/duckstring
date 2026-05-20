STATIC_DIR := src/duckstring/catchment/static
FRONTEND_OUT := frontend/out

.PHONY: build-frontend clean-frontend

build-frontend: clean-frontend
	cd frontend && NEXT_STATIC_EXPORT=true npx next build
	cp -r $(FRONTEND_OUT)/. $(STATIC_DIR)/

clean-frontend:
	find $(STATIC_DIR) -mindepth 1 -maxdepth 1 ! -name 'dev' -exec rm -rf {} +
